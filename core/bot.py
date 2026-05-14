"""
Main bot orchestrator.

Wires together:
    monitor -> [security shield] -> portfolio.open_position
    price_feed -> portfolio.decide_exit -> portfolio.execute_exit -> notifier
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
        extra_keys = [k.strip() for k in (settings.helius_api_keys or "").split(",") if k.strip()]
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
        # event loop is up (we need it to query the RPC). For paper mode we
        # use the override or a sane default.
        initial_cap = settings.initial_capital_sol_override
        if initial_cap <= 0:
            # paper-mode default: assume operator started with 1 SOL.
            # for live mode this gets overwritten before the worker loop starts.
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

        # Pipeline rate limiter: process candidates serially through the shield
        # to keep API usage predictable.
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
        # Risk manager bookkeeping:
        #   * tx failure → increment failure streak
        #   * closing event → book the position's final PnL
        #   * partial exit (2X/5X/10X with tokens remaining) → no-op until
        #     the position fully closes (avoids double-counting deltas)
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
        while True:
            token = await self._candidate_q.get()
            try:
                if not self.risk.allow_new_position():
                    # Risk brake is engaged — drop the candidate silently to
                    # avoid burning RPC on shield evaluations we won't act on.
                    continue
                if not self.portfolio.has_capacity():
                    logger.debug(f"[bot] no slot, skipping shield for {token.mint}")
                    continue
                if self.portfolio.has_position(token.mint):
                    continue

                report = await self.shield.evaluate(token)
                if not report.passed:
                    await self.notifier.notify_reject(token.mint, report.failures)
                    continue

                # Hydrate token info with price/symbol from Jupiter if possible
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

                # The actual buy. open_position raises TraderError if the
                # swap fails (live mode); we treat that as a tx failure for
                # the risk manager.
                try:
                    pos = await self.portfolio.open_position(token)
                except TraderError as e:
                    self.risk.record_tx_failure()
                    logger.warning(
                        f"[bot] buy failed for {token.symbol} ({token.mint[:8]}..): {e}"
                    )
                    continue
                if pos:
                    self.risk.record_tx_success()
                    await self.notifier.notify_buy(
                        symbol=token.symbol,
                        mint=token.mint,
                        sol=pos.sol_spent,
                        tokens=pos.initial_tokens,
                        price=pos.entry_price_usd,
                    )
            except Exception as e:
                logger.exception(f"[bot] shield_worker error: {e}")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def run(self) -> None:
        logger.info(
            f"[bot] starting in {settings.trading_mode.value.upper()} mode "
            f"size={settings.trade_size_sol} SOL slots={settings.max_open_positions}"
        )

        # For live mode, calibrate the risk manager against the actual wallet
        # balance. This makes the -50% drawdown threshold meaningful against
        # the real capital at risk. If the user set an explicit override, we
        # honor that instead.
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
                    f"[bot] could not read wallet balance for risk calibration: {e} "
                    f"(falling back to {self.risk.initial_capital_sol} SOL)"
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

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("[bot] shutdown requested")
        finally:
            for t in tasks:
                t.cancel()
            await self.shutdown()

    async def _read_wallet_sol_balance(self) -> float:
        """Query the active wallet's SOL balance. Returns 0 on failure."""
        # The LiveTrader holds the keypair + rpc client. Reach in for them.
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
