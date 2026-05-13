"""
Entry point.

Usage:
    python main.py

Make sure you have created a `.env` file (copy from `.env.example` and fill in
the keys) before running.
"""
from __future__ import annotations

import asyncio
import signal
import sys

from core.bot import Bot
from utils.logger import logger


async def _amain() -> None:
    bot = Bot()
    loop = asyncio.get_running_loop()

    stop_event = asyncio.Event()

    def _signal_handler():
        logger.warning("signal received, stopping...")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # Windows
            pass

    runner = asyncio.create_task(bot.run())
    waiter = asyncio.create_task(stop_event.wait())
    done, pending = await asyncio.wait(
        {runner, waiter}, return_when=asyncio.FIRST_COMPLETED
    )
    for t in pending:
        t.cancel()
    try:
        await asyncio.gather(*pending, return_exceptions=True)
    except Exception:
        pass


def main() -> None:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
