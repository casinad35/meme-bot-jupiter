"""
Structured logging via loguru.

Use:
    from utils.logger import logger
    logger.info("hello")
"""
from __future__ import annotations

import sys
from loguru import logger as _logger

from config import settings


def _setup() -> None:
    _logger.remove()

    # Console
    _logger.add(
        sys.stderr,
        level=settings.log_level,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
            "<level>{message}</level>"
        ),
        colorize=True,
        enqueue=True,
    )

    # File with rotation
    _logger.add(
        settings.log_path,
        level="DEBUG",
        rotation="50 MB",
        retention="14 days",
        compression="zip",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} | {message}",
        enqueue=True,
    )


_setup()
logger = _logger
