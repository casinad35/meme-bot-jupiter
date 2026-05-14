"""
Telegram notifier.

Uses the Bot HTTP API directly (no python-telegram-bot dep).

Set TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID and TELEGRAM_ENABLED=true in .env.
"""
from __future__ import annotations

import asyncio
import html
from typing import Optional

import httpx

from config import settings
from utils.logger import logger


class TelegramNotifier:
    def __init__(self):
        self.enabled = bool(
            settings.telegram_enabled
            and settings.telegram_bot_token
            and settings.telegram_chat_id
        )
        self._client = httpx.AsyncClient(timeout=8.0) if self.enabled else None
        self._url = (
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
            if self.enabled else ""
        )

    async def aclose(self) -> None:
        if self._client:
            await self._client.aclose()

    async def send(self, text: str) -> None:
        if not self.enabled or not self._client:
            return
        body = {
            "chat_id": settings.telegram_chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            r = await self._client.post(self._url, json=body)
            if r.status_code != 200:
                logger.debug(f"telegram non-200: {r.status_code} {r.text[:200]}")
        except Exception as e:
            logger.debug(f"telegram send failed: {e}")

    async def notify_buy(self, symbol: str, mint: str, sol: float, tokens: float, price: float) -> None:
        text = (
            f"🟢 <b>BUY</b> <code>{html.escape(symbol)}</code>\n"
            f"Spent: {sol:.4f} SOL\n"
            f"Got: {tokens:,.2f} tokens\n"
            f"Entry: ${price:.10f}\n"
            f"Mint: <code>{html.escape(mint)}</code>"
        )
        await self.send(text)

    async def notify_exit(self, event: dict) -> None:
        # Failed exit (tx revert, timeout, etc): brief warning instead of
        # the usual "sold for X SOL" success template. The position remains
        # open and will be retried on the next price tick.
        if event.get("failed"):
            text = (
                f"⚠️ <b>SELL FAILED ({event.get('reason', '?')})</b> "
                f"<code>{html.escape(event.get('symbol', '?'))}</code>\n"
                f"Will retry on next tick.\n"
                f"Error: {html.escape(str(event.get('error', ''))[:120])}"
            )
            await self.send(text)
            return

        # Force-closed dead pool: no real sell happened, the position was
        # abandoned because the pool was rugged/drained. Show that clearly
        # rather than printing a sale at price 0.
        if event.get("force_closed"):
            pnl = event.get("pnl_sol", 0.0)
            sign = "+" if pnl >= 0 else ""
            text = (
                f"☠️ <b>DEAD POOL (forced close)</b> "
                f"<code>{html.escape(event.get('symbol', '?'))}</code>\n"
                f"Pool drained/rugged. Position abandoned.\n"
                f"Tokens left in wallet: {event.get('tokens_remaining', 0):,.2f} "
                f"(unsellable)\n"
                f"Realized PnL: <b>{sign}{pnl:.4f} SOL</b>\n"
                f"Mint: <code>{html.escape(event.get('mint', ''))}</code>"
            )
            await self.send(text)
            return

        emoji = {
            "2x_capital_recovery": "💰",
            "5x_take_profit": "🚀",
            "10x_take_profit": "🌕",
            "trailing_stop": "🛡️",
            "hard_stop_loss": "🔻",
            "timeout": "⏰",
            "manual": "✋",
            "dead_pool": "☠️",
        }.get(event.get("reason", ""), "📤")
        pnl = event.get("pnl_sol", 0.0)
        sign = "+" if pnl >= 0 else ""
        text = (
            f"{emoji} <b>SELL ({event.get('reason', '?')})</b> "
            f"<code>{html.escape(event.get('symbol', '?'))}</code>\n"
            f"Sold: {event.get('tokens_sold', 0):,.2f} tokens\n"
            f"Got: {event.get('sol_received', 0):.4f} SOL @ "
            f"${event.get('exec_price', 0):.10f} (x{event.get('multiplier', 0):.2f})\n"
            f"Realized PnL: <b>{sign}{pnl:.4f} SOL</b>\n"
            f"Mint: <code>{html.escape(event.get('mint', ''))}</code>"
        )
        await self.send(text)

    async def notify_reject(self, mint: str, reasons: list[str]) -> None:
        # Most rejections are routine (filter is doing its job) and a stream
        # of "REJECT" Telegram messages is pure noise. Only send when the
        # operator has explicitly opted in for debugging.
        if not settings.telegram_notify_rejects:
            return
        text = (
            f"⛔ <b>REJECT</b>\n"
            f"Mint: <code>{html.escape(mint)}</code>\n"
            f"Reasons: {html.escape(', '.join(reasons[:5]))}"
        )
        await self.send(text)
