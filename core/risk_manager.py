"""
Risk management: drawdown kill-switch + transaction-failure circuit breaker.

Why this exists:
  * Paper trading is forgiving. Live trading is not. A bad streak of
    rugs/MEV/failed-execution can drain a wallet in minutes if nothing is
    watching.
  * This module is the bot's panic brake. It tracks two things:
      1. Realized PnL vs initial capital. If we've lost more than the
         configured percentage, stop opening new positions. Existing
         positions can still be closed (we'd rather exit than hold).
      2. Consecutive transaction failures. Rug-pulls, MEV sandwiches, and
         dead pools tend to produce clusters of failed swaps. If we see N
         failures in a row, pause new positions for a cooldown period.

Usage:
    risk = RiskManager(initial_capital_sol=0.5, max_drawdown_pct=0.5)
    if risk.allow_new_position():
        ... try to buy ...
    risk.record_realized_pnl_sol(+0.05)   # successful trade closed
    risk.record_tx_failure()              # a tx reverted
    risk.record_tx_success()              # a tx confirmed

The manager is intentionally a passive observer: it never raises, never
forces an exit. The bot polls allow_new_position() before each new buy.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from utils.logger import logger


@dataclass
class RiskManager:
    # Wallet SOL at bot startup. Used as the denominator for the drawdown
    # check. We never re-baseline (a recovery doesn't "unlock" the kill
    # switch — once tripped, the bot stays paused until restart, which forces
    # a human in the loop).
    initial_capital_sol: float

    # Stop opening new positions when realized PnL <= -initial * this fraction.
    # 0.5 = stop at -50% drawdown. Set to >=1.0 to disable.
    max_drawdown_pct: float = 0.5

    # Pause cooldown when too many consecutive tx failures occur.
    # Default: 3 failures in a row pauses entries for 5 minutes.
    tx_failure_threshold: int = 3
    tx_failure_cooldown_s: float = 300.0

    # Internal state
    realized_pnl_sol: float = 0.0
    _consecutive_failures: int = 0
    _cooldown_until: float = 0.0
    _drawdown_tripped: bool = False
    _trip_logged: bool = False

    def record_realized_pnl_sol(self, delta_sol: float) -> None:
        """Called when a position closes (positive or negative SOL delta)."""
        self.realized_pnl_sol += delta_sol
        if self._drawdown_breached() and not self._drawdown_tripped:
            self._drawdown_tripped = True
            logger.error(
                f"[risk] DRAWDOWN KILL SWITCH triggered: realized PnL "
                f"{self.realized_pnl_sol:+.4f} SOL on "
                f"{self.initial_capital_sol:.4f} SOL initial capital "
                f"(loss {abs(self.realized_pnl_sol) / self.initial_capital_sol:.1%} "
                f">= {self.max_drawdown_pct:.0%}). "
                f"No new positions will be opened. Existing positions can still close."
            )

    def record_tx_failure(self) -> None:
        """Called when a tx reverts, times out, or otherwise fails."""
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.tx_failure_threshold:
            self._cooldown_until = time.monotonic() + self.tx_failure_cooldown_s
            logger.warning(
                f"[risk] {self._consecutive_failures} consecutive tx failures; "
                f"pausing new positions for {self.tx_failure_cooldown_s:.0f}s"
            )

    def record_tx_success(self) -> None:
        """Called when a tx confirms. Resets the failure streak."""
        if self._consecutive_failures:
            logger.info(f"[risk] tx success, clearing failure streak ({self._consecutive_failures})")
        self._consecutive_failures = 0
        # Note: we deliberately do NOT clear self._cooldown_until here. Once
        # we enter a cooldown, we wait it out fully — a single success in
        # the middle of bad conditions isn't evidence the market is healthy.

    def allow_new_position(self) -> bool:
        """The bot's go/no-go check before opening a new position."""
        if self._drawdown_tripped:
            return False
        if time.monotonic() < self._cooldown_until:
            if not self._trip_logged:
                self._trip_logged = True
            return False
        # Re-allow once cooldown elapses
        if self._trip_logged and time.monotonic() >= self._cooldown_until:
            logger.info("[risk] cooldown elapsed, resuming new positions")
            self._trip_logged = False
        return True

    def _drawdown_breached(self) -> bool:
        if self.max_drawdown_pct >= 1.0:
            return False
        if self.initial_capital_sol <= 0:
            return False
        return self.realized_pnl_sol <= -(self.initial_capital_sol * self.max_drawdown_pct)

    def summary(self) -> str:
        return (
            f"realized={self.realized_pnl_sol:+.4f} SOL "
            f"({self.realized_pnl_sol / max(self.initial_capital_sol, 1e-9):+.1%}) "
            f"failures={self._consecutive_failures} "
            f"drawdown_tripped={self._drawdown_tripped}"
        )
