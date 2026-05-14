"""
Price feed for open positions.

For every tick we want the current spot price of every held meme so we can
ask the portfolio whether to exit. Two strategies, in order of preference:

1. **On-chain spot price** from the pool reserves. We already saved the
   sol_vault and meme_vault on the position when we entered, so each tick
   is just two `getTokenAccountBalance` calls + the SOL price. This works
   for any pool we decoded (Raydium V4, CPMM, PumpSwap), is not subject to
   Jupiter rate limits or indexing lag, and reflects the *true* spot price
   the AMM would offer.

2. **Jupiter Price API V3 fallback** for positions where we don't have
   vault info (legacy positions loaded from disk before this change), with
   exponential backoff on 429s so we don't spam our quota.

To avoid hammering Jupiter for the SOL price every tick (it's stable
enough), we cache it for SOL_PRICE_CACHE_SECONDS.

Hard timeout: see Portfolio.handle_stale_positions.
"""
from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, Optional

from config import settings
from core.portfolio import Portfolio
from models import ExitAction, ExitReason, Position, PositionStatus
from security.helius_holders import HeliusHoldersClient
from security.jupiter import JupiterClient, WSOL
from utils.logger import logger


SOL_PRICE_CACHE_SECONDS = 60.0  # SOL price doesn't move that fast

# Sanity guard for on-chain price reads: any tick beyond this multiplier
# of the entry price (or below its inverse) is treated as a transient pool
# anomaly (vault drained mid-tx, migration, etc) and discarded. 1000x in a
# single tick is implausible even for a pumping meme — real moves are
# confirmed across multiple consecutive ticks.
MAX_PLAUSIBLE_MULTIPLIER = 1000.0


class _SolPriceCache:
    """Tiny TTL cache to avoid spamming Jupiter for SOL price every tick.

    Also implements exponential backoff after 429s so a Jupiter outage
    doesn't generate hundreds of failed log lines per minute.
    """
    def __init__(self):
        self._value: Optional[float] = None
        self._fetched_at: float = 0.0
        # Backoff state for 429s
        self._next_attempt_at: float = 0.0
        self._failure_streak: int = 0

    async def get(self, jupiter: JupiterClient) -> Optional[float]:
        now = time.monotonic()
        if self._value and (now - self._fetched_at) < SOL_PRICE_CACHE_SECONDS:
            return self._value
        if now < self._next_attempt_at:
            # We're in backoff; reuse stale cached value (better than nothing)
            return self._value
        try:
            p = await jupiter.price(WSOL)
        except Exception as e:
            p = None
            logger.debug(f"[pricefeed] sol price fetch errored: {e}")
        if p and p > 0:
            self._value = p
            self._fetched_at = now
            self._failure_streak = 0
            self._next_attempt_at = 0.0
            return p
        # Failure -> exponential backoff: 5s, 10s, 20s, 40s, max 120s
        self._failure_streak += 1
        backoff = min(5.0 * (2 ** (self._failure_streak - 1)), 120.0)
        self._next_attempt_at = now + backoff
        if self._failure_streak == 1 or self._failure_streak % 5 == 0:
            logger.warning(
                f"[pricefeed] SOL price unavailable, backing off {backoff:.0f}s "
                f"(streak={self._failure_streak}, last_known=${self._value})"
            )
        return self._value  # may be None on first failure


class PriceFeed:
    def __init__(
        self,
        portfolio: Portfolio,
        jupiter: JupiterClient,
        holders: HeliusHoldersClient,
        on_exit_event: Callable[[dict], Awaitable[None]],
        poll_interval: float = 4.0,
    ):
        self.portfolio = portfolio
        self.jupiter = jupiter
        self.holders = holders
        self.on_exit_event = on_exit_event
        self.poll_interval = poll_interval
        self._stop = asyncio.Event()
        self._sol_price = _SolPriceCache()

    async def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as e:
                logger.exception(f"[pricefeed] tick error: {e}")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        if not self.portfolio.positions:
            return

        # Get SOL price once for the whole tick. If unavailable, on-chain
        # pricing can't compute USD values, so we'll fall back to Jupiter
        # per-mint for those.
        sol_price = await self._sol_price.get(self.jupiter)

        for mint, position in list(self.portfolio.positions.items()):
            price = await self._price_for_position(position, sol_price)
            if price is None or price <= 0:
                # Check for stuck-position force-close: when the sanity
                # rejection streak crosses the threshold, the pool is dead
                # (drained / migrated / rugged) and the position will never
                # exit via normal triggers. Force-close it.
                if (
                    position.absurd_price_streak >= settings.absurd_price_force_close_ticks
                    and position.status != PositionStatus.CLOSED
                ):
                    await self._force_close_dead_pool(position)
                continue

            action = Portfolio.decide_exit(position, price)
            if action is None:
                continue

            try:
                event = await self.portfolio.execute_exit(position, action)
            except Exception as e:
                # Sell failed — surface the failure but don't crash the
                # tick loop. The position stays open and we'll retry on
                # the next price update.
                logger.warning(
                    f"[pricefeed] exit {action.reason.value} failed for "
                    f"{position.token.symbol}: {e}"
                )
                # The bot wires on_exit_event to also count failures; we
                # send a synthetic event so the risk manager can react.
                try:
                    await self.on_exit_event({
                        "mint": mint,
                        "symbol": position.token.symbol,
                        "reason": action.reason.value,
                        "failed": True,
                        "error": str(e),
                        "closing": False,
                    })
                except Exception as e2:
                    logger.debug(f"[pricefeed] failure-event handler error: {e2}")
                continue
            if event:
                try:
                    await self.on_exit_event(event)
                except Exception as e:
                    logger.exception(f"[pricefeed] on_exit_event failed: {e}")

    async def _force_close_dead_pool(self, position: Position) -> None:
        """
        The price-feed sanity check has rejected the on-chain price for too
        many consecutive ticks. The pool is effectively dead (drained,
        migrated, or rugged). We need to free the slot and book the loss
        so the risk manager sees it.

        Strategy:
          1. Try one sell attempt via Jupiter. If a market exists at all we
             may recover a few percent. The portfolio's TraderError handling
             will surface failures gracefully.
          2. If the sell fails (no route, all reverts), mark the position
             closed in memory with whatever it has actually realized so far.
             The remaining tokens stay in the wallet (worthless) but the
             slot is freed and the risk manager books the negative PnL.

        We only do this once per position — the absurd_price_streak counter
        is set to a large negative value after the attempt, so even another
        300 absurd ticks won't retrigger.
        """
        mint = position.token.mint
        symbol = position.token.symbol
        logger.error(
            f"[pricefeed] FORCE-CLOSE dead pool: {symbol} ({mint[:8]}..) "
            f"after {position.absurd_price_streak} consecutive absurd-price ticks. "
            f"Attempting last-ditch sell, then booking the loss."
        )
        # Prevent re-trigger this cycle no matter what happens below.
        position.absurd_price_streak = -10_000_000

        # Step 1: one sell attempt at DEAD_POOL exit reason. The portfolio
        # uses Jupiter quotes for live sells, so if no route exists Jupiter
        # will return an error and execute_exit will raise — we catch and
        # move on to step 2.
        action = ExitAction(
            reason=ExitReason.DEAD_POOL,
            fraction_of_initial=1.0,
            trigger_price_usd=None,
        )
        try:
            event = await self.portfolio.execute_exit(position, action)
        except Exception as e:
            logger.warning(
                f"[pricefeed] dead-pool sell failed for {symbol}: {e}"
            )
            event = None

        if event:
            # Sell actually went through — surface it via the normal path.
            try:
                await self.on_exit_event(event)
            except Exception as e:
                logger.exception(f"[pricefeed] dead-pool on_exit_event failed: {e}")
            return

        # Step 2: book the position as closed in memory. We don't have a
        # confirmed sell, so we set sol_out = 0 effectively (realized_sol
        # unchanged). PnL = realized_sol - sol_spent = -sol_spent at worst.
        await self._book_dead_position(position)

    async def _book_dead_position(self, position: Position) -> None:
        """
        Mark a position as closed without any further sell, using whatever
        SOL was already realized from prior partial exits. Emit a synthetic
        exit event so notifier + risk manager see the closure.
        """
        mint = position.token.mint
        if mint not in self.portfolio.positions:
            return
        # Move to history under the portfolio lock to stay consistent with
        # the normal close path.
        async with self.portfolio._lock:
            if mint not in self.portfolio.positions:
                return
            position.status = PositionStatus.CLOSED
            from datetime import datetime, timezone
            position.closed_at = datetime.now(timezone.utc)
            # We didn't sell anything in this step, so tokens_held stays as
            # whatever the wallet still owns. PnL reflects only what was
            # actually realized (likely 0 for a stuck-from-day-1 position).
            self.portfolio.history.append(position)
            self.portfolio.positions.pop(mint, None)
            self.portfolio._save()

        pnl = position.realized_pnl_sol()
        event = {
            "mint": mint,
            "symbol": position.token.symbol,
            "reason": ExitReason.DEAD_POOL.value,
            "tokens_sold": 0.0,
            "sol_received": 0.0,
            "exec_price": 0.0,
            "multiplier": 0.0,
            "tokens_remaining": position.tokens_held,
            "realized_sol": position.realized_sol,
            "pnl_sol": pnl,
            "closing": True,
            "force_closed": True,
        }
        logger.error(
            f"[pricefeed] booked {position.token.symbol} as DEAD_POOL closure: "
            f"realized {pnl:+.4f} SOL, {position.tokens_held:,.2f} tokens "
            f"abandoned in wallet (unsellable)"
        )
        try:
            await self.on_exit_event(event)
        except Exception as e:
            logger.exception(f"[pricefeed] dead-pool booking event failed: {e}")

    async def _price_for_position(
        self, position: Position, sol_price: Optional[float]
    ) -> Optional[float]:
        """
        Return the current USD price per meme token for a position.

        Strategy 1: on-chain (sol_vault and meme_vault known + SOL price known)
        Strategy 2: Jupiter Price API V3 (with cache to dampen 429s)

        Sanity check: if the on-chain price differs from the entry price by
        more than `MAX_PLAUSIBLE_MULTIPLIER`, we assume the pool was reset /
        migrated / drained (vault balance temporarily near zero produces
        astronomical prices) and ignore the tick. Real meme pumps are
        confirmed by multiple consecutive ticks anyway.
        """
        token = position.token

        # Strategy 1: on-chain
        if token.sol_vault and token.meme_vault and sol_price and sol_price > 0:
            try:
                sol_balance = await self.holders.get_token_account_balance(token.sol_vault)
                meme_balance = await self.holders.get_token_account_balance(token.meme_vault)
            except Exception as e:
                logger.debug(f"[pricefeed] on-chain read failed for {token.mint[:8]}: {e}")
                sol_balance = meme_balance = None
            if (
                sol_balance is not None and sol_balance > 0
                and meme_balance is not None and meme_balance > 0
            ):
                price = (sol_balance * sol_price) / meme_balance
                # Sanity: reject implausible jumps caused by transient pool
                # state (vault drained mid-block, migration, etc).
                if position.entry_price_usd > 0:
                    ratio = price / position.entry_price_usd
                    if ratio > MAX_PLAUSIBLE_MULTIPLIER or ratio < 1.0 / MAX_PLAUSIBLE_MULTIPLIER:
                        position.absurd_price_streak += 1
                        logger.warning(
                            f"[pricefeed] rejecting absurd price for {token.symbol} "
                            f"({token.mint[:8]}..): {price:.2e} = x{ratio:.0f} entry "
                            f"(sol_bal={sol_balance}, meme_bal={meme_balance}) "
                            f"streak={position.absurd_price_streak}"
                        )
                        return None
                # Healthy read — reset the streak so a single bad tick mid-trade
                # doesn't accumulate toward a force-close.
                position.absurd_price_streak = 0
                return price

        # Strategy 2: Jupiter (rate-limited, used only when on-chain is impossible)
        try:
            prices = await self.jupiter.prices_multi([token.mint])
        except Exception as e:
            logger.debug(f"[pricefeed] jupiter price failed for {token.mint[:8]}: {e}")
            return None
        return prices.get(token.mint)
