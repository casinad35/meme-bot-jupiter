"""
Main bot orchestrator.

Wires together:
    monitor -> [security shield] -> portfolio.open_position
    price_feed -> portfolio.decide_exit -> portfolio.execute_exit -> notifier

Patched from the GitHub version to:
  * Surface every silent candidate drop (risk brake, no slot, shield reject)
    at INFO level, not debug — so a non-trading bot is diagnosable from
    the logs alone.
  * Attach a done_callback to every long-running task so a crashed task
    is logged, instead of swallowed by asyncio.
"""
from __future__ import annotations

import asyncio
from typing import Optional

import httpx

from config import settings
from core.monitor import PoolMonitor
from core.portfolio import Portfolio
from core.price_feed import PriceFeed
from core.risk_manager import RiskManager
from core.trader import make_trader, TraderError
from models import TokenInfo
from notifications.telegram import TelegramNotifier
from security.jupiter import JupiterClient
from security.goplus import GoPlusClient
from security.helius_holders import HeliusHoldersClient
from security.shield import Shield
from utils.logger import logger


class Bot:
    def __init__(self):
        # Build clients
        self.jupiter = JupiterClient(settings.jupiter_api_key)
        self.goplus = GoPlusClient(settings.goplus_api_key)
        # Build the list of Helius RPC URLs for failover. The primary URL
        # is settings.solana_rpc_url; additional keys (settings.helius_api_keys,
        # a comma-separated list of API keys) are appended as failover URLs.
        rpc_urls = [settings.solana_rpc_url]
        extra_keys = [
            k.strip() for k in (settings.helius_api_keys or "").split(",") if k.strip()
        ]
        for key in extra_keys:
            rpc_urls.append(f"https://mainnet.helius-rpc.com/?api-key={key}")
        self.holders = HeliusHoldersClient(rpc_urls)

        # Derive WebSocket URLs from every RPC URL we have (https:// → wss://).
        ws_urls = [url.replace("https://", "wss://") for url in rpc_urls]

        self.shield = Shield(self.goplus, self.jupiter, self.holders)
        self.trader = make_trader(settings.solana_rpc_url, self.jupiter)
        self.portfolio = Portfolio(self.trader)
        self.notifier = TelegramNotifier()

        # Risk manager — initialized with a placeholder capital. For live mode
        # we replace it with the actual wallet balance in run(), once the
        # event loop is up.
        initial_cap = settings.initial_capital_sol_override
        if initial_cap <= 0:
            initial_cap = 1.0
        self.risk = RiskManager(
            initial_capital_sol=initial_cap,
            max_drawdown_pct=settings.max_drawdown_pct,
            tx_failure_threshold=settings.tx_failure_threshold,
            tx_failure_cooldown_s=settings.tx_failure_cooldown_s,
        )

        self.monitor = PoolMonitor(
            ws_url=ws_urls,
            rpc_url=settings.solana_rpc_url,
            on_candidate=self._on_candidate,
        )
        self.price_feed = PriceFeed(
            portfolio=self.portfolio,
            jupiter=self.jupiter,
            holders=self.holders,
            on_exit_event=self._on_exit_event,
        )

        # Pipeline rate limiter
        self._candidate_q: asyncio.Queue[TokenInfo] = asyncio.Queue(maxsize=200)

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------
    async def _on_candidate(self, token: TokenInfo) -> None:
        try:
            self._candidate_q.put_nowait(token)
        except asyncio.QueueFull:
            logger.warning(f"[bot] candidate queue full, dropping {token.mint}")

    async def _on_exit_event(self, event: dict) -> None:
        if event.get("failed"):
            self.risk.record_tx_failure()
        else:
            self.risk.record_tx_success()
            if event.get("closing"):
                pnl = float(event.get("pnl_sol") or 0.0)
                self.risk.record_realized_pnl_sol(pnl)
                logger.info(f"[bot] risk status: {self.risk.summary()}")
        await self.notifier.notify_exit(event)

    # ------------------------------------------------------------------
    # Worker that drains the candidate queue
    # ------------------------------------------------------------------
    async def _shield_worker(self) -> None:
        seen = 0
        while True:
            token = await self._candidate_q.get()
            seen += 1
            mint8 = token.mint[:8]
            try:
                # --- Risk brake -------------------------------------------------
                if not self.risk.allow_new_position():
                    logger.info(
                        f"[bot] #{seen} drop {mint8}.. — risk brake engaged "
                        f"({self.risk.summary()})"
                    )
                    continue
                # --- Capacity ---------------------------------------------------
                if not self.portfolio.has_capacity():
                    logger.info(
                        f"[bot] #{seen} drop {mint8}.. — no open slot "
                        f"(positions={self.portfolio.open_count() if hasattr(self.portfolio, 'open_count') else '?'})"
                    )
                    continue
                if self.portfolio.has_position(token.mint):
                    logger.info(
                        f"[bot] #{seen} drop {mint8}.. — already holding"
                    )
                    continue

                # --- Shield -----------------------------------------------------
                logger.info(f"[bot] #{seen} shield evaluating {mint8}..")
                report = await self.shield.evaluate(token)
                if not report.passed:
                    # Promote reject visibility — used to go only to telegram.
                    logger.info(
                        f"[bot] #{seen} shield REJECT {mint8}.. — "
                        f"reasons={report.failures}"
                    )
                    await self.notifier.notify_reject(token.mint, report.failures)
                    continue
                logger.info(f"[bot] #{seen} shield PASS {mint8}.. — buying")

                # --- Hydrate with Jupiter --------------------------------------
                overview = await self.jupiter.token_overview(token.mint)
                if overview:
                    token.symbol = overview.get("symbol") or token.symbol
                    token.name = overview.get("name") or token.name
                    try:
                        if overview.get("decimals") is not None:
                            token.decimals = int(overview["decimals"])
                    except Exception:
                        pass
                    try:
                        token.price_usd = float(overview.get("price") or 0.0)
                    except Exception:
                        pass
                    try:
                        token.liquidity_usd = float(overview.get("liquidity") or 0.0)
                    except Exception:
                        pass

                # --- Buy --------------------------------------------------------
                try:
                    pos = await self.portfolio.open_position(token)
                except TraderError as e:
                    self.risk.record_tx_failure()
                    logger.warning(
                        f"[bot] #{seen} buy FAILED {token.symbol} ({mint8}..): {e}"
                    )
                    continue
                if pos:
                    self.risk.record_tx_success()
                    logger.info(
                        f"[bot] #{seen} buy OK {token.symbol} ({mint8}..) "
                        f"sol={pos.sol_spent} tokens={pos.initial_tokens}"
                    )
                    await self.notifier.notify_buy(
                        symbol=token.symbol,
                        mint=token.mint,
                        sol=pos.sol_spent,
                        tokens=pos.initial_tokens,
                        price=pos.entry_price_usd,
                    )
            except Exception as e:
                logger.exception(f"[bot] #{seen} shield_worker error: {e}")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def run(self) -> None:
        logger.info(
            f"[bot] starting in {settings.trading_mode.value.upper()} mode "
            f"size={settings.trade_size_sol} SOL slots={settings.max_open_positions}"
        )

        if (
            settings.trading_mode.value == "live"
            and settings.initial_capital_sol_override <= 0
        ):
            try:
                bal_sol = await self._read_wallet_sol_balance()
                if bal_sol > 0:
                    self.risk.initial_capital_sol = bal_sol
                    logger.info(
                        f"[bot] live wallet balance: {bal_sol:.4f} SOL — "
                        f"risk manager calibrated against this baseline "
                        f"(max drawdown: {settings.max_drawdown_pct:.0%})"
                    )
            except Exception as e:
                logger.warning(
                    f"[bot] could not read wallet balance for risk calibration: "
                    f"{e} (falling back to {self.risk.initial_capital_sol} SOL)"
                )

        await self.notifier.send(
            f"🤖 Bot started ({settings.trading_mode.value}) | "
            f"size={settings.trade_size_sol} SOL slots={settings.max_open_positions} "
            f"capital={self.risk.initial_capital_sol:.4f} SOL "
            f"max_dd={settings.max_drawdown_pct:.0%}"
        )

        tasks = [
            asyncio.create_task(self.monitor.run(), name="monitor"),
            asyncio.create_task(self._shield_worker(), name="shield_worker"),
            asyncio.create_task(self.price_feed.run(), name="price_feed"),
        ]
        # Belt-and-suspenders: log if any of these dies for an unhandled reason.
        for t in tasks:
            t.add_done_callback(self._task_done)

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("[bot] shutdown requested")
        finally:
            for t in tasks:
                t.cancel()
            await self.shutdown()

    @staticmethod
    def _task_done(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.exception(
                f"[bot] task {task.get_name()!r} crashed: {exc!r}",
                exc_info=exc,
            )

    async def _read_wallet_sol_balance(self) -> float:
        trader = self.trader
        if not hasattr(trader, "rpc") or not hasattr(trader, "keypair"):
            return 0.0
        try:
            resp = await trader.rpc.get_balance(trader.keypair.pubkey())
            lamports = int(resp.value or 0)
            return lamports / 1_000_000_000
        except Exception:
            return 0.0

    async def shutdown(self) -> None:
        await self.price_feed.stop()
        await self.monitor.aclose()
        await self.goplus.aclose()
        await self.jupiter.aclose()
        await self.holders.aclose()
        await self.notifier.aclose()
        await self.trader.aclose()
        logger.info("[bot] shutdown complete")
