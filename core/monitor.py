"""
Pool Monitor.

Subscribes via Solana JSON-RPC WebSocket (`logsSubscribe`) to logs from the
Raydium AMM v4 and Raydium CPMM programs. When we see a pool initialization
log, we:

  1. Fetch the transaction
  2. Extract the new pool address and token mints
  3. Skip the SOL/USDC side, identify the meme mint
  4. Push a TokenInfo candidate to a queue for the rest of the pipeline

This file does the *minimum* parsing necessary to get the candidate token
mint. Deeper validation happens in the shield.

If you want to watch pump.fun graduations, pump.fun migrations to PumpSwap,
or Orca whirlpools, add their program IDs to PROGRAMS_TO_WATCH and adapt
the log filter strings.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

import httpx
import websockets

from models import TokenInfo
from utils.logger import logger


RAYDIUM_AMM_V4 = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
RAYDIUM_CPMM = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
PUMPSWAP_AMM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
PUMP_FUN = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

# Quote mints we *don't* want to treat as the meme side
QUOTE_MINTS = {
    "So11111111111111111111111111111111111111112",  # wSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}

# Strings to look for in tx logs that indicate a new pool was initialized.
# Match is case-insensitive and substring-based to survive minor log changes.
#
# Coverage:
#   * Raydium AMM v4: prints "initialize2: InitializeInstruction2 { ... }"
#   * Raydium CPMM:   prints "Instruction: Initialize" (Anchor)
#   * PumpSwap:       prints "Instruction: CreatePool" on graduation (Anchor)
#   * Pump.fun:       prints "Instruction: Migrate" when a token graduates
#                     to PumpSwap. Useful as an early signal because the
#                     pool exists ~1 block later.
POOL_INIT_HINTS = (
    "initialize2",       # Raydium AMM v4
    "initialize_pool",   # CPMM (Anchor snake_case)
    "initializepool",    # CPMM (some toolchains drop the underscore)
    "createpool",        # PumpSwap (Anchor)
    "create_pool",       # PumpSwap snake_case
    "init_pool",
    "migrate",           # Pump.fun -> PumpSwap graduation
)


def _looks_like_pool_init(logs: list[str]) -> bool:
    """Case-insensitive scan of tx logs for any pool-init hint."""
    for line in logs:
        low = line.lower()
        for hint in POOL_INIT_HINTS:
            if hint in low:
                return True
    return False


class PoolMonitor:
    """
    Async iterator-like monitor: yields TokenInfo candidates via callback.
    """

    def __init__(
        self,
        ws_url: str | list[str],
        rpc_url: str,
        on_candidate,  # async callable(TokenInfo) -> None
        programs: Optional[list[str]] = None,
    ):
        # Accept a single URL (legacy) or a list of URLs for WS failover.
        # Helius rate-limits WebSocket connections per key; rotating across
        # keys on 429 mirrors what HeliusHoldersClient does for HTTP RPC.
        if isinstance(ws_url, str):
            self._ws_urls: list[str] = [ws_url]
        else:
            self._ws_urls = list(ws_url) if ws_url else []
        if not self._ws_urls:
            raise ValueError("PoolMonitor requires at least one WS URL")
        # Expose the first URL for backward-compat code that reads .ws_url
        self.ws_url: str = self._ws_urls[0]
        self._ws_idx: int = 0          # index of the currently active WS URL
        self._ws_429_streak: int = 0   # consecutive 429s across all keys

        self.rpc_url = rpc_url
        self.on_candidate = on_candidate
        self.programs = programs or [
            RAYDIUM_AMM_V4,
            RAYDIUM_CPMM,
            PUMPSWAP_AMM,
            PUMP_FUN,
        ]
        self._http = httpx.AsyncClient(timeout=10.0)
        self._stop = asyncio.Event()
        self._next_id = 0
        self._seen_mints: set[str] = set()

    async def aclose(self) -> None:
        self._stop.set()
        await self._http.aclose()

    async def run(self) -> None:
        while not self._stop.is_set():
            current_url = self._ws_urls[self._ws_idx % len(self._ws_urls)]
            try:
                async with websockets.connect(
                    current_url, ping_interval=20, ping_timeout=20, max_size=2**22
                ) as ws:
                    # Successful connection — reset 429 state
                    if self._ws_429_streak > 0:
                        logger.info(
                            f"[monitor] WS connected on key#{self._ws_idx % len(self._ws_urls)}, "
                            f"429 streak cleared"
                        )
                    self._ws_429_streak = 0
                    self.ws_url = current_url
                    await self._subscribe_all(ws)
                    await self._listen(ws)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                err_str = str(e)
                is_429 = "429" in err_str

                if is_429 and len(self._ws_urls) > 1:
                    # Rotate to the next key before sleeping
                    old_idx = self._ws_idx % len(self._ws_urls)
                    self._ws_idx += 1
                    new_idx = self._ws_idx % len(self._ws_urls)
                    self._ws_429_streak += 1
                    # Exponential backoff: 5s, 10s, 20s … capped at 60s
                    backoff = min(5.0 * (2 ** (self._ws_429_streak - 1)), 60.0)
                    logger.warning(
                        f"[monitor] WS 429 on key#{old_idx} (streak={self._ws_429_streak}); "
                        f"rotating to key#{new_idx}, backoff={backoff:.0f}s"
                    )
                    await asyncio.sleep(backoff)
                else:
                    # Non-429 error (close frame, timeout, etc.) or single key:
                    # don't rotate, just reconnect after a short pause.
                    self._ws_429_streak = 0
                    logger.warning(f"[monitor] WS error: {e}; reconnecting in 5s")
                    await asyncio.sleep(5)

    async def _subscribe_all(self, ws) -> None:
        for prog in self.programs:
            self._next_id += 1
            sub = {
                "jsonrpc": "2.0",
                "id": self._next_id,
                "method": "logsSubscribe",
                "params": [
                    {"mentions": [prog]},
                    {"commitment": "confirmed"},
                ],
            }
            await ws.send(json.dumps(sub))
            logger.info(f"[monitor] subscribed to {prog}")

    async def _listen(self, ws) -> None:
        # Diagnostic counters so a silent monitor is visible in logs
        msgs_seen = 0
        candidates_seen = 0
        # Sample a few raw log payloads each minute to surface what's actually
        # arriving. Helps diagnose silent monitors when no candidates fire.
        sample_logs: list[str] = []
        sample_target = 3
        last_report = asyncio.get_event_loop().time()

        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            params = msg.get("params")
            if not params:
                continue
            value = params.get("result", {}).get("value", {})
            logs = value.get("logs") or []
            sig = value.get("signature")
            if not logs or not sig:
                continue
            if value.get("err"):
                continue
            msgs_seen += 1

            # Capture a few sample log lines per heartbeat window so we can see
            # what kinds of logs are coming through if the candidate count stays
            # at zero. We pick the most informative line ("Instruction:" or any
            # init-shaped string) when present; otherwise the first non-trivial
            # line.
            if len(sample_logs) < sample_target:
                interesting: Optional[str] = None
                for line in logs:
                    if "instruction" in line.lower() or "initialize" in line.lower():
                        interesting = line
                        break
                if interesting is None and logs:
                    interesting = logs[0]
                if interesting:
                    sample_logs.append(interesting[:200])

            # Heartbeat every 60s so we know the WS is actually alive
            now = asyncio.get_event_loop().time()
            if now - last_report >= 60:
                logger.info(
                    f"[monitor] heartbeat: {msgs_seen} log msgs, "
                    f"{candidates_seen} candidates in last {int(now - last_report)}s"
                )
                if candidates_seen == 0 and sample_logs:
                    for i, sl in enumerate(sample_logs):
                        logger.info(f"[monitor] sample log {i+1}: {sl}")
                msgs_seen = 0
                candidates_seen = 0
                sample_logs = []
                last_report = now

            if not _looks_like_pool_init(logs):
                continue
            candidates_seen += 1
            asyncio.create_task(self._handle_pool_creation(sig))

    async def _handle_pool_creation(self, signature: str) -> None:
        try:
            tx = await self._fetch_tx(signature)
        except Exception as e:
            logger.debug(f"[monitor] tx fetch failed {signature}: {e}")
            return
        if not tx:
            return
        candidate = self._extract_candidate(tx)
        if not candidate:
            return
        if candidate.mint in self._seen_mints:
            return
        self._seen_mints.add(candidate.mint)
        # Cap memory
        if len(self._seen_mints) > 10000:
            self._seen_mints = set(list(self._seen_mints)[-5000:])
        logger.info(
            f"[monitor] new pool={candidate.pool_address} mint={candidate.mint}"
        )
        try:
            await self.on_candidate(candidate)
        except Exception as e:
            logger.exception(f"[monitor] on_candidate handler failed: {e}")

    async def _fetch_tx(self, signature: str) -> Optional[dict[str, Any]]:
        self._next_id += 1
        body = {
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": "getTransaction",
            "params": [
                signature,
                {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
                 "commitment": "confirmed"},
            ],
        }
        # Retry briefly: the RPC may not have indexed it yet
        for delay in (0.5, 1.0, 2.0):
            try:
                r = await self._http.post(self.rpc_url, json=body)
                r.raise_for_status()
                payload = r.json()
            except httpx.HTTPError:
                await asyncio.sleep(delay)
                continue
            result = payload.get("result")
            if result:
                return result
            await asyncio.sleep(delay)
        return None

    def _extract_candidate(self, tx: dict[str, Any]) -> Optional[TokenInfo]:
        """
        Walk the parsed instructions; the new pool init carries the two mints
        and the new pool account. We pick whichever mint isn't a known quote
        mint, and we identify the pool address by finding the account whose
        owner program is one of our watched AMMs.
        """
        msg = tx.get("transaction", {}).get("message", {})
        meta = tx.get("meta", {}) or {}

        # Collect every account key referenced in the tx
        all_keys: list[str] = []
        for k in msg.get("accountKeys", []):
            if isinstance(k, dict):
                pk = k.get("pubkey")
            else:
                pk = k
            if pk:
                all_keys.append(pk)

        # Mints involved in the tx, from postTokenBalances
        post_balances = meta.get("postTokenBalances") or []
        mints = {b.get("mint") for b in post_balances if b.get("mint")}
        mints.discard(None)
        if not mints:
            return None

        meme_mints = [m for m in mints if m not in QUOTE_MINTS]
        if not meme_mints:
            return None
        meme_mint = meme_mints[0]

        # ---- Pool address resolution ----
        #
        # Strategy: walk the parsed instructions; for each watched program
        # invocation, the *first* unique non-mint account is the pool/state
        # account. We also track which program owns it.
        #
        # We deliberately SKIP Pump.fun bonding curves (program 6EF8rrec...).
        # Reason: a Pump.fun bonding curve is a separate state account, NOT
        # a tradable AMM pool. The actual tradable pool only exists once the
        # token graduates and a PumpSwap pool is created — which we capture
        # separately via the PumpSwap subscription. Forwarding bonding curve
        # accounts here just produces "unsupported owner" decode failures.
        watched = {RAYDIUM_AMM_V4, RAYDIUM_CPMM, PUMPSWAP_AMM, PUMP_FUN}
        tradable = {RAYDIUM_AMM_V4, RAYDIUM_CPMM, PUMPSWAP_AMM}
        pool: str = ""
        pool_source_program: Optional[str] = None

        def _walk_ix(ix: dict[str, Any]) -> Optional[tuple[str, str]]:
            prog = ix.get("programId")
            if prog not in watched:
                return None
            accounts = ix.get("accounts") or []
            for acc in accounts:
                if (
                    acc
                    and acc not in mints
                    and acc not in QUOTE_MINTS
                ):
                    return (acc, prog)
            return None

        for ix in msg.get("instructions", []) or []:
            cand = _walk_ix(ix) if isinstance(ix, dict) else None
            if cand:
                pool, pool_source_program = cand
                break
        if not pool:
            for inner in meta.get("innerInstructions") or []:
                for ix in inner.get("instructions") or []:
                    cand = _walk_ix(ix) if isinstance(ix, dict) else None
                    if cand:
                        pool, pool_source_program = cand
                        break
                if pool:
                    break

        # If the only program touched here is Pump.fun (bonding curve), skip.
        # We'll re-evaluate the same mint when its PumpSwap pool appears.
        if pool_source_program == PUMP_FUN:
            logger.debug(
                f"[monitor] skipping Pump.fun bonding curve for mint={meme_mint} "
                f"(will re-evaluate on PumpSwap pool creation)"
            )
            return None

        # Last-resort fallback: previous heuristic. Better than empty so the
        # rest of the pipeline still gets a chance, but the on-chain pool
        # decode will likely fail and the shield will mark layout-unreadable.
        # Exclude well-known program IDs that always appear in account lists
        # but are never the pool address (SPL Token program, System program,
        # ATA program, ComputeBudget, etc.).
        WELL_KNOWN_PROGRAMS = {
            "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",  # SPL Token
            "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",  # Token-2022
            "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",  # ATA
            "11111111111111111111111111111111",              # System
            "ComputeBudget111111111111111111111111111111",   # ComputeBudget
            "SysvarRent111111111111111111111111111111111",   # Rent sysvar
            "SysvarC1ock11111111111111111111111111111111",   # Clock sysvar
            RAYDIUM_AMM_V4, RAYDIUM_CPMM, PUMPSWAP_AMM, PUMP_FUN,
        }
        if not pool:
            for k in all_keys:
                if (
                    k != meme_mint
                    and k not in QUOTE_MINTS
                    and k not in mints
                    and k not in WELL_KNOWN_PROGRAMS
                ):
                    pool = k
                    break

        return TokenInfo(mint=meme_mint, pool_address=pool)
