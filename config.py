"""
Configuration loader.

Reads values from the .env file and validates them. All other modules import
the singleton `settings` from this module rather than reading env vars directly.
"""
from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class TradingMode(str, Enum):
    PAPER = "paper"
    LIVE = "live"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Mode
    trading_mode: TradingMode = TradingMode.PAPER

    # Solana RPC / WS
    helius_api_key: str = ""
    # Optional comma-separated list of additional Helius keys for failover.
    # When the active key gets 429-rate-limited or exhausts its quota, the
    # client rotates to the next key. Format: "key1,key2,key3"
    helius_api_keys: str = ""
    solana_rpc_url: str = "https://api.mainnet-beta.solana.com"
    solana_ws_url: str = "wss://api.mainnet-beta.solana.com"

    # Wallet
    wallet_private_key_base58: Optional[str] = None
    wallet_keypair_path: Optional[str] = None

    # Security APIs
    goplus_api_key: Optional[str] = None
    jupiter_api_key: Optional[str] = None

    # Risk management
    # Maximum realized drawdown (as fraction of initial wallet SOL) before the
    # bot stops opening new positions. 0.5 = stop at -50%. Set to 1.0 to disable.
    max_drawdown_pct: float = 0.5
    # Number of consecutive failed swap transactions that triggers a cooldown.
    tx_failure_threshold: int = 3
    # Cooldown duration after a failure streak, in seconds.
    tx_failure_cooldown_s: float = 300.0
    # Override the auto-detected initial capital (read from wallet at startup).
    # Useful for paper mode or when you want to size risk against a virtual
    # bankroll smaller than the actual wallet. 0 = auto-detect.
    initial_capital_sol_override: float = 0.0

    # Telegram
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    telegram_enabled: bool = False
    # Whether to send a Telegram message for every REJECT verdict from the
    # shield. Default false because the shield rejects 95%+ of candidates and
    # the resulting message volume is noise, not signal. Set to true via
    # TELEGRAM_NOTIFY_REJECTS=true when debugging the filter logic.
    telegram_notify_rejects: bool = False

    # Trading params
    trade_size_sol: float = 0.1
    max_open_positions: int = 3
    slippage_bps: int = 1500
    priority_fee_microlamports: int = 200_000
    hard_stop_loss_ratio: float = 0.5
    max_hold_minutes: int = 240

    # Security filters
    max_top_holder_pct: float = 0.05
    # How many top holders to walk through when computing concentration.
    # Each holder costs 2 RPC (token-account->wallet, then wallet program
    # owner check). The top user holder is virtually always within the top
    # 10 after filtering out the LP/program-owned vaults, so 10 is a good
    # balance between robustness and RPC cost.
    holders_to_inspect: int = 10
    # Liquidity range. Pump.fun tokens graduate at ~12k USD liquidity, so
    # 8k is a sane floor that doesn't reject fresh graduations. Raise this
    # if you only want more mature pools.
    min_liquidity_usd: float = 8_000
    max_liquidity_usd: float = 2_000_000
    min_early_buys: int = 3
    max_pool_age_seconds: int = 120
    # If True, reject candidates whose LP lock/burn status can't be proven on
    # chain (i.e. the shield's lp_status step returned `None`). Without proof
    # of locked/burnt LP, the pool creator can drain liquidity at any moment
    # — which is exactly how the WorldCup rug played out in our first live
    # run. Setting this to False reverts to a warning and lets those tokens
    # through; only do that if you understand the risk.
    require_lp_lock_proof: bool = True

    # When the price feed rejects on-chain price as "absurd" (sanity check
    # caught a drained vault / migration / etc.) for this many consecutive
    # ticks, force-close the position. At 4s/tick, 30 ticks = 2 minutes of
    # garbage prices, which is well past any plausible transient state.
    # If Jupiter can still quote a sell, we try it; otherwise we book the
    # position as a total loss to free the slot.
    absurd_price_force_close_ticks: int = 30

    # Cluster check is the most expensive shield step (~30 RPC calls per
    # token). Disabled by default to save Helius quota; the on-chain
    # concentration check already catches the most common rugs.
    enable_cluster_check: bool = False

    # Shield-level cache: skip re-evaluating mints we've seen recently
    # (a single mint can show up across multiple pools in a short window).
    shield_cache_seconds: int = 60

    # Runtime
    log_level: str = "INFO"
    log_file: str = "logs/bot.log"
    data_dir: str = "data"

    @field_validator("trade_size_sol")
    @classmethod
    def _check_size(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("TRADE_SIZE_SOL must be > 0")
        if v > 5:
            # Sanity check - meme bots should risk small amounts
            raise ValueError("TRADE_SIZE_SOL > 5 looks like a typo. Refusing to start.")
        return v

    @property
    def data_path(self) -> Path:
        p = Path(self.data_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def log_path(self) -> Path:
        p = Path(self.log_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p


# Singleton
settings = Settings()
