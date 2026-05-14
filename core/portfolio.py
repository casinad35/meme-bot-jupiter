"""
Portfolio: tracks open positions and decides when to exit.

Exit strategy ("Hit & Run"):
  * At 2x: sell 50% of the *initial* tokens (capital recovery)
  * After 2x: trailing stop at 20% drawdown from peak
  * At 5x: sell additional 10% of initial tokens
  * At 10x: sell additional 10% of initial tokens
  * Optional hard stop loss before 2x (configurable)
  * Optional max-hold timeout (configurable)

The portfolio is the single source of truth for "what we hold". Trader is
told *what* to sell/buy; portfolio updates state once trader returns.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import settings
from core.trader import BaseTrader, TraderError, _fmt_price
from models import ExitAction, ExitReason, Position, PositionStatus, TokenInfo
from utils.logger import logger


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Portfolio:
    """In-memory portfolio with disk persistence."""

    def __init__(self, trader: BaseTrader, state_path: Optional[Path] = None):
        self.trader = trader
        self.positions: dict[str, Position] = {}
        self.history: list[Position] = []
        self.state_path = state_path or (settings.data_path / "portfolio.json")
        self._lock = asyncio.Lock()
        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text())
        except Exception as e:
            logger.warning(f"failed to load portfolio state: {e}")
            return
        # We deliberately don't try to rehydrate full Position objects; on
        # restart the bot ignores prior in-flight positions to avoid acting on
        # stale data. We just keep the history.
        self.history = []  # could deserialize if you want

    def _save(self) -> None:
        try:
            payload = {
                "open": [p.to_dict() for p in self.positions.values()],
                "closed": [p.to_dict() for p in self.history[-100:]],
            }
            self.state_path.write_text(json.dumps(payload, indent=2))
        except Exception as e:
            logger.warning(f"failed to save portfolio state: {e}")

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------
    def has_capacity(self) -> bool:
        return len(self.positions) < settings.max_open_positions

    def has_position(self, mint: str) -> bool:
        return mint in self.positions

    # ------------------------------------------------------------------
    # Open a new position
    # ------------------------------------------------------------------
    async def open_position(self, token: TokenInfo) -> Optional[Position]:
        async with self._lock:
            if not self.has_capacity():
                logger.info(f"[portfolio] no slot free, skipping {token.symbol}")
                return None
            if self.has_position(token.mint):
                logger.info(f"[portfolio] already holding {token.symbol}")
                return None

            try:
                hint = token.price_usd if token.price_usd > 0 else None
                tokens, exec_price = await self.trader.buy(
                    token.mint, settings.trade_size_sol, hint_price_usd=hint
                )
            except TraderError as e:
                logger.error(f"[portfolio] buy failed for {token.symbol}: {e}")
                return None
            except Exception as e:
                logger.exception(f"[portfolio] unexpected buy error: {e}")
                return None

            pos = Position(
                token=token,
                entry_price_usd=exec_price,
                sol_spent=settings.trade_size_sol,
                initial_tokens=tokens,
                tokens_held=tokens,
                peak_price_usd=exec_price,
            )
            self.positions[token.mint] = pos
            self._save()
            logger.success(
                f"[portfolio] OPEN {token.symbol} {tokens:,.2f} tokens @ {_fmt_price(exec_price)} "
                f"({len(self.positions)}/{settings.max_open_positions} slots)"
            )
            return pos

    # ------------------------------------------------------------------
    # Decide exit
    # ------------------------------------------------------------------
    @staticmethod
    def decide_exit(position: Position, current_price: float) -> Optional[ExitAction]:
        if position.status == PositionStatus.CLOSED:
            return None
        if current_price <= 0 or position.entry_price_usd <= 0:
            return None

        multiplier = current_price / position.entry_price_usd
        if current_price > position.peak_price_usd:
            position.peak_price_usd = current_price

        # 2x capital recovery
        if not position.has_taken_2x and multiplier >= 2.0:
            return ExitAction(
                reason=ExitReason.CAPITAL_RECOVERY_2X,
                fraction_of_initial=0.50,
                trigger_price_usd=current_price,
            )

        # Tiered TP only after 2x already taken (otherwise we'd skip the 50% step)
        if position.has_taken_2x:
            if not position.has_taken_5x and multiplier >= 5.0:
                return ExitAction(
                    reason=ExitReason.TAKE_PROFIT_5X,
                    fraction_of_initial=0.10,
                    trigger_price_usd=current_price,
                )
            if not position.has_taken_10x and multiplier >= 10.0:
                return ExitAction(
                    reason=ExitReason.TAKE_PROFIT_10X,
                    fraction_of_initial=0.10,
                    trigger_price_usd=current_price,
                )

            # Trailing stop on remaining bag (20% drawdown from peak)
            if position.peak_price_usd > 0:
                drawdown = (position.peak_price_usd - current_price) / position.peak_price_usd
                if drawdown >= 0.20:
                    return ExitAction(
                        reason=ExitReason.TRAILING_STOP,
                        fraction_of_initial=1.0,
                        trigger_price_usd=current_price,
                    )

        # Hard stop loss before 2x
        if (
            not position.has_taken_2x
            and settings.hard_stop_loss_ratio > 0
            and multiplier <= settings.hard_stop_loss_ratio
        ):
            return ExitAction(
                reason=ExitReason.HARD_STOP_LOSS,
                fraction_of_initial=1.0,
                trigger_price_usd=current_price,
            )

        # Timeout
        held_minutes = (_now() - position.opened_at).total_seconds() / 60
        if held_minutes >= settings.max_hold_minutes:
            return ExitAction(
                reason=ExitReason.TIMEOUT,
                fraction_of_initial=1.0,
                trigger_price_usd=current_price,
            )

        return None

    # ------------------------------------------------------------------
    # Execute exit
    # ------------------------------------------------------------------
    async def execute_exit(self, position: Position, action: ExitAction) -> Optional[dict]:
        async with self._lock:
            mint = position.token.mint
            if mint not in self.positions:
                return None

            # How many tokens to sell
            target_tokens = position.initial_tokens * action.fraction_of_initial
            # Don't oversell what we still have
            tokens_to_sell = min(target_tokens, position.tokens_held)
            if tokens_to_sell <= 0:
                return None

            try:
                sol_out, exec_price = await self.trader.sell(
                    mint, tokens_to_sell, hint_price_usd=action.trigger_price_usd
                )
            except TraderError as e:
                # Surface the failure to the caller — the risk manager wants
                # to count it, and the operator wants to see it in alerts.
                # The position stays open; we'll retry on the next price tick.
                logger.error(f"[portfolio] sell failed {position.token.symbol}: {e}")
                raise
            except Exception as e:
                logger.exception(f"[portfolio] unexpected sell error: {e}")
                raise TraderError(f"unexpected sell error: {e}")

            position.tokens_held -= tokens_to_sell
            position.realized_sol += sol_out
            mult = exec_price / position.entry_price_usd if position.entry_price_usd else 0

            # Update milestones
            if action.reason == ExitReason.CAPITAL_RECOVERY_2X:
                position.has_taken_2x = True
                position.status = PositionStatus.PARTIAL_EXIT
            elif action.reason == ExitReason.TAKE_PROFIT_5X:
                position.has_taken_5x = True
            elif action.reason == ExitReason.TAKE_PROFIT_10X:
                position.has_taken_10x = True

            # Closing actions
            closing = action.reason in (
                ExitReason.TRAILING_STOP,
                ExitReason.HARD_STOP_LOSS,
                ExitReason.TIMEOUT,
                ExitReason.MANUAL,
                ExitReason.DEAD_POOL,
            ) or position.tokens_held <= position.initial_tokens * 0.001

            event = {
                "mint": mint,
                "symbol": position.token.symbol,
                "reason": action.reason.value,
                "tokens_sold": tokens_to_sell,
                "sol_received": sol_out,
                "exec_price": exec_price,
                "multiplier": mult,
                "tokens_remaining": position.tokens_held,
                "realized_sol": position.realized_sol,
                "pnl_sol": position.realized_pnl_sol(),
                # True iff this exit closes the position. Consumers (risk
                # manager, notifiers) use this to know when to book the
                # final PnL vs treat as a partial.
                "closing": bool(closing),
            }

            logger.success(
                f"[portfolio] {action.reason.value.upper()} {position.token.symbol} "
                f"sold {tokens_to_sell:,.2f} -> {sol_out:.4f} SOL "
                f"@ {_fmt_price(exec_price)} (x{mult:.2f}) "
                f"realized PnL: {event['pnl_sol']:+.4f} SOL"
            )

            if closing:
                position.status = PositionStatus.CLOSED
                position.closed_at = _now()
                self.history.append(position)
                self.positions.pop(mint, None)

            self._save()
            return event
