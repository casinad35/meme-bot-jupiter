"""
Domain data models (plain dataclasses).

Keep these small and serializable; they get passed between async tasks.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PositionStatus(str, Enum):
    OPEN = "open"
    PARTIAL_EXIT = "partial_exit"
    CLOSED = "closed"


class ExitReason(str, Enum):
    CAPITAL_RECOVERY_2X = "2x_capital_recovery"
    TAKE_PROFIT_5X = "5x_take_profit"
    TAKE_PROFIT_10X = "10x_take_profit"
    TRAILING_STOP = "trailing_stop"
    HARD_STOP_LOSS = "hard_stop_loss"
    TIMEOUT = "timeout"
    MANUAL = "manual"
    # Pool was drained (likely rug) — sanity check has been rejecting the
    # on-chain price for too many consecutive ticks. Force-close to free
    # the slot, even if the actual sell can't recover anything.
    DEAD_POOL = "dead_pool"


@dataclass
class TokenInfo:
    """Basic info about a meme token candidate."""
    mint: str
    symbol: str = "?"
    name: str = "?"
    decimals: int = 6
    pool_address: str = ""
    liquidity_usd: float = 0.0
    price_usd: float = 0.0
    # Pool vaults — populated by the shield from on-chain decode. Used by
    # the price feed so it can read the spot price directly from the pool
    # reserves (avoiding Jupiter rate limits and indexing lag).
    sol_vault: str = ""
    meme_vault: str = ""
    pool_kind: str = ""
    detected_at: datetime = field(default_factory=_now)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["detected_at"] = self.detected_at.isoformat()
        return d


@dataclass
class SecurityReport:
    """Outcome of all security checks against a token."""
    token_mint: str
    passed: bool = False
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # Individual checks (None = not checked)
    is_honeypot: Optional[bool] = None
    mint_authority_renounced: Optional[bool] = None
    freeze_authority_renounced: Optional[bool] = None
    lp_locked_or_burnt: Optional[bool] = None
    top_holder_pct: Optional[float] = None
    cluster_detected: Optional[bool] = None
    liquidity_usd: Optional[float] = None

    raw: dict = field(default_factory=dict)

    def add_fail(self, reason: str) -> None:
        self.failures.append(reason)

    def add_warn(self, reason: str) -> None:
        self.warnings.append(reason)

    def passed_far(self) -> bool:
        """True iff no hard failure has been recorded yet (used for short-
        circuiting sequential checks before finalize() is called)."""
        return len(self.failures) == 0

    def finalize(self) -> "SecurityReport":
        self.passed = len(self.failures) == 0
        return self


@dataclass
class ExitAction:
    """A decision produced by the exit strategy."""
    reason: ExitReason
    fraction_of_initial: float  # 0..1, fraction of the *initial* token amount
    trigger_price_usd: Optional[float] = None  # the price that triggered this exit


@dataclass
class Position:
    token: TokenInfo
    entry_price_usd: float
    sol_spent: float
    initial_tokens: float
    tokens_held: float

    status: PositionStatus = PositionStatus.OPEN

    # Milestones already taken
    has_taken_2x: bool = False
    has_taken_5x: bool = False
    has_taken_10x: bool = False

    # Trailing stop tracking (in USD, only active after 2x exit)
    peak_price_usd: float = 0.0

    # Realized SOL from sales (for PnL)
    realized_sol: float = 0.0

    # Counter for consecutive price-feed ticks where the on-chain price was
    # rejected as absurd (sanity check). When this crosses a threshold, the
    # position is force-closed: a drained vault that never recovers is a
    # rug, and a stuck position prevents the slot from being reused.
    absurd_price_streak: int = 0

    opened_at: datetime = field(default_factory=_now)
    closed_at: Optional[datetime] = None

    @property
    def multiplier(self) -> float:
        """Current multiplier vs entry price (uses last seen peak as fallback)."""
        return (self.peak_price_usd / self.entry_price_usd) if self.entry_price_usd else 0.0

    def realized_pnl_sol(self) -> float:
        """SOL profit realized so far (negative if losing)."""
        return self.realized_sol - self.sol_spent

    def to_dict(self) -> dict:
        d = {
            "token": self.token.to_dict(),
            "entry_price_usd": self.entry_price_usd,
            "sol_spent": self.sol_spent,
            "initial_tokens": self.initial_tokens,
            "tokens_held": self.tokens_held,
            "status": self.status.value,
            "has_taken_2x": self.has_taken_2x,
            "has_taken_5x": self.has_taken_5x,
            "has_taken_10x": self.has_taken_10x,
            "peak_price_usd": self.peak_price_usd,
            "realized_sol": self.realized_sol,
            "opened_at": self.opened_at.isoformat(),
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
        }
        return d
