"""
On-chain holder analysis using Helius / Solana RPC.

Two responsibilities:

1) Top-holder concentration:
   We pull the largest token accounts via `getTokenLargestAccounts`, look up
   the wallet behind each, and exclude the LP pool / locker / common burn
   sinks. If any single non-LP wallet holds more than `max_top_holder_pct`
   of supply, the token is rejected.

2) Cluster detection (lightweight Bubble-Maps-style):
   For the top-N holders we look at how their wallet was funded -- specifically
   the SOL transfer that created/funded it. If many top holders share the same
   funder OR were funded within a tight time window from the same source, that
   strongly suggests a coordinated team/rug setup and the token is rejected.

3) LP burn / lock check (added later):
   For a given Raydium pool address we decode the pool layout to extract the
   LP mint, then look at who holds the LP. If >=95% sits at the SPL incinerator
   or a known locker program, LP is treated as burnt/locked.

This is intentionally a *cheap* heuristic. Real Bubble Maps does multi-hop
graph analysis; here we do a one-hop funding check, which catches the common
case where an attacker funds 10 fresh wallets from the same source wallet to
hold a big chunk of supply.
"""
from __future__ import annotations

from base64 import b64decode
from collections import Counter
from typing import Any, Optional

import base58
import httpx

from utils.logger import logger


# Known sinks/lockers to exclude when computing concentration
KNOWN_NON_USER_OWNERS = {
    "11111111111111111111111111111111",  # System program (shouldn't hold tokens)
    "1nc1nerator11111111111111111111111111111111",  # SPL incinerator
    "1111111111111111111111111111111111111111",  # malformed null-like
}

# Addresses that count as a "burn" when they hold LP tokens
KNOWN_BURN_ADDRESSES = {
    "1nc1nerator11111111111111111111111111111111",  # canonical SPL incinerator
    "11111111111111111111111111111111",              # system program (some bots send LP here)
}

# Known LP locker / vesting programs. If a token account's owner is a PDA of
# one of these programs, the LP is effectively locked.
KNOWN_LOCKER_PROGRAMS = {
    "strmRqUCoQUgGUan5YhzUZa6KqdzwX5L6FpUxfmKg5m",  # Streamflow
    "LocpQgucEQHbqNABEYvBvwoxCPsSbG91A1QaQhQQqjn",  # Meteora Lock (LP locker)
}

# Known LP / AMM / locker programs (their PDAs hold pool tokens) - we skip them
# when judging concentration.
KNOWN_LP_PROGRAM_OWNERS = {
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",  # Raydium AMM v4
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",  # Raydium CPMM
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",   # Orca Whirlpool
    "9W959DqEETiGZocYWCQPaJ6sBmUzgfxXfqGeTEdp3aQP",  # Orca v1
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",   # PumpSwap AMM
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",   # Pump.fun
}


# Pool layout offsets (verified against Raydium SDK layouts).
#
# Raydium AMM v4 LiquidityStateV4: 32 u64 fields (256B) + 5 swap fields (80B,
# ending at 336) + baseVault, quoteVault, baseMint, quoteMint, lpMint
# (32B each, ending at 496).
#   baseVault   = bytes 336..368
#   quoteVault  = bytes 368..400
#   baseMint    = bytes 400..432
#   quoteMint   = bytes 432..464
#   lpMint      = bytes 464..496
RAYDIUM_AMM_V4_PROGRAM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
RAYDIUM_AMM_V4_BASE_VAULT_OFFSET = 336
RAYDIUM_AMM_V4_QUOTE_VAULT_OFFSET = 368
RAYDIUM_AMM_V4_BASE_MINT_OFFSET = 400
RAYDIUM_AMM_V4_QUOTE_MINT_OFFSET = 432
RAYDIUM_AMM_V4_LP_MINT_OFFSET = 464

# Raydium CPMM PoolState: 8B Anchor discriminator + ammConfig(32) +
# poolCreator(32) + token0Vault(32) + token1Vault(32) + lpMint(32) +
# token0Mint(32) + token1Mint(32).
#   token0Vault = bytes 72..104
#   token1Vault = bytes 104..136
#   lpMint      = bytes 136..168
#   token0Mint  = bytes 168..200
#   token1Mint  = bytes 200..232
RAYDIUM_CPMM_PROGRAM = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
RAYDIUM_CPMM_TOKEN0_VAULT_OFFSET = 72
RAYDIUM_CPMM_TOKEN1_VAULT_OFFSET = 104
RAYDIUM_CPMM_LP_MINT_OFFSET = 136
RAYDIUM_CPMM_TOKEN0_MINT_OFFSET = 168
RAYDIUM_CPMM_TOKEN1_MINT_OFFSET = 200

# PumpSwap pool layout (Pool struct, Anchor):
#   8B  Anchor discriminator
#   1B  pool_bump
#   2B  index
#   32B creator
#   32B base_mint
#   32B quote_mint
#   32B lp_mint
#   32B pool_base_token_account     (= base vault)
#   32B pool_quote_token_account    (= quote vault)
#   ... rest (lp_supply, fees, etc) — not needed
# Offsets:
#   base_mint   = bytes 43..75
#   quote_mint  = bytes 75..107
#   lp_mint     = bytes 107..139
#   base_vault  = bytes 139..171
#   quote_vault = bytes 171..203
PUMPSWAP_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
PUMPSWAP_BASE_MINT_OFFSET = 43
PUMPSWAP_QUOTE_MINT_OFFSET = 75
PUMPSWAP_LP_MINT_OFFSET = 107
PUMPSWAP_BASE_VAULT_OFFSET = 139
PUMPSWAP_QUOTE_VAULT_OFFSET = 171

# Programs whose PDAs are considered "as-good-as-renounced" mint authorities.
# When a mint's authority is a PDA owned by one of these programs, it means
# the mint is controlled by a published smart contract (Pump.fun bonding
# curve / PumpSwap pool), not a human keypair. The supply is fixed by the
# protocol, so leaving authority on the program is equivalent to a true
# renouncement from a rug-risk standpoint.
TRUSTED_AUTHORITY_PROGRAMS = {
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",  # Pump.fun
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",  # PumpSwap AMM
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",  # Raydium AMM v4
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",  # Raydium CPMM
}


# Wrapped SOL mint (used to identify which vault holds SOL)
WSOL_MINT = "So11111111111111111111111111111111111111112"

# Token program IDs. `getTokenLargestAccounts` works for the classic SPL
# Token program only; Token-2022 mints will return -32602 on that endpoint.
SPL_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


class HeliusHoldersClient:
    """
    Wraps Solana RPC calls used for holder/cluster analysis.

    Supports multiple RPC URLs (Helius keys from different accounts) with
    automatic failover. When the active URL gets 429-rate-limited, we rotate
    to the next URL. Only when ALL URLs are gated do we apply backoff.

    This dramatically extends the bot's runtime on the free Helius tier:
    with N keys you get ~N× the quota before having to wait.
    """

    def __init__(self, rpc_url: str | list[str], timeout: float = 8.0):
        # Accept either a single URL (legacy) or a list of URLs (failover mode).
        if isinstance(rpc_url, str):
            self._rpc_urls: list[str] = [rpc_url]
        else:
            self._rpc_urls = list(rpc_url) if rpc_url else []
        if not self._rpc_urls:
            raise ValueError("HeliusHoldersClient requires at least one RPC URL")
        # Per-URL backoff state. When a URL is rate-limited we mark its
        # "until" timestamp; we only fall back to the global gate when ALL
        # URLs are currently gated.
        import time as _t
        self._url_state: list[dict[str, float | int]] = [
            {"until": 0.0, "streak": 0} for _ in self._rpc_urls
        ]
        # Index of the currently active URL (rotates on 429)
        self._active_idx: int = 0
        # First-time public attribute for backward compat (some code may
        # introspect .rpc_url; we expose the currently active one).
        self.rpc_url: str = self._rpc_urls[0]
        self._client = httpx.AsyncClient(timeout=timeout)
        self._next_id = 0
        # Global gate: only tripped when EVERY URL is rate-limited at once.
        self._gate_until: float = 0.0
        self._gate_streak: int = 0
        # JSON-RPC error code from the last call (or None on success/non-RPC error)
        self._last_error_code: Optional[int] = None
        if len(self._rpc_urls) > 1:
            # Mask the keys in logs (assume "?api-key=<uuid>" in URL)
            masked = [self._mask_url(u) for u in self._rpc_urls]
            logger.info(
                f"[helius] failover enabled across {len(self._rpc_urls)} keys: {masked}"
            )

    @staticmethod
    def _mask_url(url: str) -> str:
        """Mask the api-key in a Helius URL for safe logging."""
        if "api-key=" not in url:
            return url
        before, _, after = url.partition("api-key=")
        return before + "api-key=" + (after[:4] + "..." + after[-4:] if len(after) > 8 else "***")

    def _pick_url(self) -> Optional[int]:
        """
        Choose an available URL index. Returns None if all URLs are gated.
        Strategy: stick with the active one if it's available, otherwise
        rotate to the first non-gated URL.
        """
        import time as _t
        now = _t.monotonic()
        # If active is still good, keep it (avoid unnecessary rotation)
        if self._url_state[self._active_idx]["until"] <= now:
            return self._active_idx
        # Otherwise scan for any non-gated URL
        for i in range(len(self._rpc_urls)):
            idx = (self._active_idx + 1 + i) % len(self._rpc_urls)
            if self._url_state[idx]["until"] <= now:
                old = self._active_idx
                self._active_idx = idx
                self.rpc_url = self._rpc_urls[idx]
                logger.info(
                    f"[helius] rotating from key#{old} to key#{idx} "
                    f"({self._mask_url(self._rpc_urls[idx])})"
                )
                return idx
        return None  # all URLs are gated

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _rpc(self, method: str, params: list[Any]) -> Optional[Any]:
        """
        Returns the parsed `result` field on success, or None on failure.

        Side effects:
          * 429 on the active URL → marks that URL as gated and rotates to
            the next available one. Only when ALL URLs are gated do we
            return None immediately (global backoff).
          * Other RPC errors are logged but don't trip any gate.
          * Sets self._last_error_code to the JSON-RPC error code (or None
            on success / network errors), so callers can short-circuit
            retries on permanent errors like -32602 "not a Token mint".
        """
        import time as _t
        self._last_error_code = None

        idx = self._pick_url()
        if idx is None:
            # All URLs are gated — synthesize an error code so callers know
            self._last_error_code = -429
            return None

        url = self._rpc_urls[idx]
        self._next_id += 1
        body = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}
        try:
            r = await self._client.post(url, json=body)
        except httpx.HTTPError as e:
            logger.debug(f"RPC {method} network error on key#{idx}: {e}")
            return None
        if r.status_code == 429:
            # Gate just THIS URL — exponential backoff per-URL.
            state = self._url_state[idx]
            state["streak"] = int(state["streak"]) + 1
            backoff = min(5.0 * (2 ** (state["streak"] - 1)), 300.0)
            state["until"] = _t.monotonic() + backoff
            self._last_error_code = -429
            if state["streak"] == 1 or state["streak"] % 5 == 0:
                logger.warning(
                    f"[helius] key#{idx} rate limited "
                    f"(streak={state['streak']}); pausing this key for {backoff:.0f}s"
                )
            # Trigger immediate rotation: next _rpc call will _pick_url anew.
            return None
        if r.status_code >= 400:
            logger.debug(f"RPC {method} HTTP {r.status_code} on key#{idx}")
            return None
        try:
            payload = r.json()
        except Exception as e:
            logger.debug(f"RPC {method} bad JSON on key#{idx}: {e}")
            return None
        if "error" in payload:
            err = payload["error"] or {}
            self._last_error_code = err.get("code")
            logger.debug(f"RPC {method} error on key#{idx}: {err}")
            return None
        # Success: clear THIS URL's backoff streak.
        state = self._url_state[idx]
        if int(state["streak"]) > 0:
            logger.info(f"[helius] key#{idx} rate limit cleared, resuming")
            state["streak"] = 0
            state["until"] = 0.0
        return payload.get("result")

    async def get_token_supply(self, mint: str) -> Optional[float]:
        """
        Returns token ui-supply. Retries with `processed` commitment because
        a freshly-created mint may not be visible at `confirmed` for ~1 slot
        after the mint creation tx.

        Bails out immediately on JSON-RPC error -32602 ("Invalid param") —
        that means the address isn't a token mint at all (e.g. it's a
        Token-2022 mint, or our extraction picked the wrong account), so
        retrying won't help.
        """
        import asyncio
        for delay in (0.0, 0.4, 1.0, 2.0):
            if delay:
                await asyncio.sleep(delay)
            result = await self._rpc(
                "getTokenSupply",
                [mint, {"commitment": "processed"}],
            )
            if self._last_error_code == -32602:
                # Permanent — not a Token mint
                return None
            if not result:
                continue
            amount = result.get("value", {}).get("uiAmount")
            if amount is None:
                continue
            try:
                return float(amount)
            except Exception:
                return None
        return None

    async def get_largest_accounts(self, mint: str) -> list[dict[str, Any]]:
        """
        Returns top token holders (token accounts, not wallets). Same
        retry/processed pattern as get_token_supply.

        IMPORTANT: getTokenLargestAccounts on standard Solana RPC only works
        for the classic SPL Token program. Token-2022 mints will return
        -32602 "Invalid param: not a Token mint" — we detect that and bail
        out with [], so callers can recognize the limitation.
        """
        import asyncio
        for delay in (0.0, 0.4, 1.0, 2.0):
            if delay:
                await asyncio.sleep(delay)
            result = await self._rpc(
                "getTokenLargestAccounts",
                [mint, {"commitment": "processed"}],
            )
            if self._last_error_code == -32602:
                # Permanent — likely a Token-2022 mint, no point retrying
                return []
            if not result:
                continue
            value = result.get("value")
            if value:
                return value
        return []

    async def get_account_owner(self, address: str) -> Optional[str]:
        """For a token account, returns the wallet that owns it. Retries
        for the same race-condition reasons as get_token_supply."""
        import asyncio
        for delay in (0.0, 0.4, 1.0):
            if delay:
                await asyncio.sleep(delay)
            result = await self._rpc(
                "getAccountInfo",
                [address, {"encoding": "jsonParsed", "commitment": "processed"}],
            )
            if not result:
                continue
            value = result.get("value")
            if value is None:
                continue
            parsed = value.get("data", {}).get("parsed", {})
            info = parsed.get("info", {}) if isinstance(parsed, dict) else {}
            owner = info.get("owner")
            if owner:
                return owner
            # parsed exists but has no owner -> this isn't a token account, no point retrying
            return None
        return None

    async def get_account_program_owner(self, address: str) -> Optional[str]:
        """
        Returns the program that owns the account.

        Used to (a) classify large LP holders as locker PDAs and (b) classify
        a mint's authority as a trusted-program PDA (Pump.fun / PumpSwap /
        Raydium). Both contexts can hit fresh accounts created in the same
        slot we're querying — hence `processed` + brief retry.
        """
        import asyncio
        for delay in (0.0, 0.4, 1.0):
            if delay:
                await asyncio.sleep(delay)
            result = await self._rpc(
                "getAccountInfo",
                [address, {"encoding": "base64", "commitment": "processed"}],
            )
            if not result:
                continue
            value = result.get("value")
            if value is None:
                continue
            return value.get("owner")
        return None

    async def get_mint_info(self, mint: str) -> Optional[dict[str, Any]]:
        """
        Returns parsed mint info including mintAuthority and freezeAuthority.

        Returns:
          * dict with at least the mint info fields: success
          * None: account doesn't exist OR exists but isn't a parseable mint
            (caller should treat as a hard fail — `cannot_read_mint_account`).

        We retry briefly because the monitor often picks up a tx in the
        *same slot* the mint was created. A `confirmed` RPC may not have
        the account yet for a few hundred ms. Three attempts at 0.4 / 1.0 /
        2.0 s buys us ~1 slot of indexing latency without slowing the
        happy-path noticeably.

        We also defend against the case where the address is NOT a mint
        (e.g. our extraction picked a token account or a pool state PDA by
        mistake): jsonParsed will still return data for those, but without
        the "mint" parsed type. Returning None in that case prevents the
        shield from silently treating a non-mint as "authorities renounced".
        """
        last_err: Optional[str] = None
        for delay in (0.0, 0.4, 1.0, 2.0):
            if delay:
                import asyncio
                await asyncio.sleep(delay)
            # `processed` is fast and sufficient: a freshly-created mint is
            # at the *same slot* the pool tx landed in, so `confirmed` (a few
            # hundred ms behind the tip) often misses it. Mints don't roll
            # back, so reading at `processed` is safe here.
            result = await self._rpc(
                "getAccountInfo",
                [mint, {"encoding": "jsonParsed", "commitment": "processed"}],
            )
            if not result:
                last_err = "rpc_no_result"
                continue
            value = result.get("value")
            if value is None:
                last_err = "account_not_found"
                continue
            data = value.get("data") or {}
            # data may be dict (jsonParsed) or list (base64 fallback)
            if not isinstance(data, dict):
                last_err = "data_not_parsed"
                # Account exists but RPC couldn't parse it — likely not a
                # mint. Do not retry; this isn't a timing issue.
                logger.debug(f"[mint-info] {mint}: data not parsed (likely not a mint)")
                return None
            parsed = data.get("parsed")
            if not isinstance(parsed, dict):
                # Account exists but no parsed payload -> definitely not a
                # standard mint (Token / Token-2022 both yield parsed dicts).
                logger.debug(f"[mint-info] {mint}: no parsed payload (not a mint?)")
                return None
            ptype = parsed.get("type")
            if ptype != "mint":
                # parsed.type is e.g. "account" for a token account, "multisig"
                # for a multisig — none of which we want.
                logger.debug(
                    f"[mint-info] {mint}: parsed.type={ptype!r} (not a mint)"
                )
                return None
            info = parsed.get("info", {})
            if isinstance(info, dict) and info:
                # Inject the account-level owner (= the token program ID)
                # so callers can tell SPL Token from Token-2022 without an
                # extra RPC call.
                info["_program_owner"] = value.get("owner")
                # Also expose `parsed.program` ("spl-token" or "spl-token-2022")
                info["_parsed_program"] = parsed.get("program")
                return info
            last_err = "empty_parsed_info"
            return None
        logger.debug(f"[mint-info] {mint}: gave up after retries ({last_err})")
        return None

    async def get_funding_source(self, wallet: str) -> Optional[tuple[str, int]]:
        """
        Find the wallet that funded `wallet` (cheap one-hop heuristic).

        We look at the *oldest* signatures for the address and find the first
        SOL transfer where it was the receiver. Returns (sender, slot) or None.
        """
        sigs = await self._rpc(
            "getSignaturesForAddress",
            [wallet, {"limit": 1000}],
        )
        if not sigs:
            return None
        # Oldest first
        sigs = list(reversed(sigs))
        # Inspect the first few
        for s in sigs[:5]:
            sig = s.get("signature")
            if not sig:
                continue
            tx = await self._rpc(
                "getTransaction",
                [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
            )
            if not tx:
                continue
            slot = tx.get("slot", 0)
            msg = tx.get("transaction", {}).get("message", {})
            for ix in msg.get("instructions", []):
                parsed = ix.get("parsed", {}) if isinstance(ix, dict) else {}
                if not isinstance(parsed, dict):
                    continue
                if parsed.get("type") in ("transfer", "transferChecked"):
                    info = parsed.get("info", {})
                    if info.get("destination") == wallet and info.get("source"):
                        return (info["source"], slot)
        return None

    # ------------------------------------------------------------------
    # Pool decoding: extract LP mint + vaults + base/quote mints in 1 RPC
    # ------------------------------------------------------------------
    async def decode_pool(self, pool_address: str) -> Optional[dict[str, Any]]:
        """
        Decode a pool account in a single getAccountInfo call.

        Supports Raydium AMM v4, Raydium CPMM, and PumpSwap. Returns a dict:
            pool_kind   : "raydium_amm_v4" | "raydium_cpmm" | "pumpswap"
            lp_mint     : str
            base_vault  : str   (vault holding the meme/base token)
            quote_vault : str   (vault holding the quote token, usually WSOL)
            base_mint   : str
            quote_mint  : str
            sol_vault   : str | None   (whichever vault holds WSOL, if any)
        Or None if the program is unsupported / data is too short.

        Brief retry with `processed` commitment because the pool PDA is often
        created in the same slot we're querying — `confirmed` may not have it
        yet and would return value=None.
        """
        import asyncio
        last_err: Optional[str] = None
        for delay in (0.0, 0.4, 1.0, 2.0):
            if delay:
                await asyncio.sleep(delay)
            result = await self._rpc(
                "getAccountInfo",
                [pool_address, {"encoding": "base64", "commitment": "processed"}],
            )
            if not result:
                last_err = "rpc_no_result"
                continue
            value = result.get("value")
            if value is None:
                last_err = "account_not_found"
                continue
            decoded = self._decode_pool_value(pool_address, value)
            if decoded is not None:
                return decoded
            # Layout unsupported / data too short — not a timing issue, stop.
            return None
        logger.debug(f"[decode_pool] {pool_address}: gave up ({last_err})")
        return None

    def _decode_pool_value(
        self, pool_address: str, value: dict[str, Any]
    ) -> Optional[dict[str, Any]]:
        """Pure-CPU side of decode_pool; takes the rpc value dict."""
        program_owner = value.get("owner")
        data_field = value.get("data")
        if not isinstance(data_field, list) or len(data_field) < 1:
            return None
        try:
            raw = b64decode(data_field[0])
        except Exception:
            return None

        if program_owner == RAYDIUM_AMM_V4_PROGRAM:
            kind = "raydium_amm_v4"
            offsets = {
                "base_vault": RAYDIUM_AMM_V4_BASE_VAULT_OFFSET,
                "quote_vault": RAYDIUM_AMM_V4_QUOTE_VAULT_OFFSET,
                "base_mint": RAYDIUM_AMM_V4_BASE_MINT_OFFSET,
                "quote_mint": RAYDIUM_AMM_V4_QUOTE_MINT_OFFSET,
                "lp_mint": RAYDIUM_AMM_V4_LP_MINT_OFFSET,
            }
        elif program_owner == RAYDIUM_CPMM_PROGRAM:
            kind = "raydium_cpmm"
            offsets = {
                "base_vault": RAYDIUM_CPMM_TOKEN0_VAULT_OFFSET,
                "quote_vault": RAYDIUM_CPMM_TOKEN1_VAULT_OFFSET,
                "lp_mint": RAYDIUM_CPMM_LP_MINT_OFFSET,
                "base_mint": RAYDIUM_CPMM_TOKEN0_MINT_OFFSET,
                "quote_mint": RAYDIUM_CPMM_TOKEN1_MINT_OFFSET,
            }
        elif program_owner == PUMPSWAP_PROGRAM:
            kind = "pumpswap"
            offsets = {
                "base_mint": PUMPSWAP_BASE_MINT_OFFSET,
                "quote_mint": PUMPSWAP_QUOTE_MINT_OFFSET,
                "lp_mint": PUMPSWAP_LP_MINT_OFFSET,
                "base_vault": PUMPSWAP_BASE_VAULT_OFFSET,
                "quote_vault": PUMPSWAP_QUOTE_VAULT_OFFSET,
            }
        else:
            logger.debug(
                f"[decode_pool] {pool_address}: unsupported owner {program_owner}"
            )
            return None

        max_off = max(offsets.values())
        if len(raw) < max_off + 32:
            logger.debug(
                f"[decode_pool] {pool_address}: data too short ({len(raw)}B) for {kind}"
            )
            return None

        out: dict[str, Any] = {"pool_kind": kind}
        zero32 = bytes(32)
        for name, off in offsets.items():
            chunk = raw[off : off + 32]
            if chunk == zero32:
                out[name] = None
            else:
                try:
                    out[name] = base58.b58encode(chunk).decode("ascii")
                except Exception:
                    out[name] = None

        # Identify which vault holds SOL so callers don't have to.
        if out.get("base_mint") == WSOL_MINT:
            out["sol_vault"] = out.get("base_vault")
            out["sol_side"] = "base"
        elif out.get("quote_mint") == WSOL_MINT:
            out["sol_vault"] = out.get("quote_vault")
            out["sol_side"] = "quote"
        else:
            out["sol_vault"] = None
            out["sol_side"] = None

        return out

    async def get_pool_lp_mint(self, pool_address: str) -> Optional[tuple[str, str]]:
        """Backward-compatible wrapper. Prefer decode_pool() for new code."""
        info = await self.decode_pool(pool_address)
        if not info:
            return None
        lp = info.get("lp_mint")
        kind = info.get("pool_kind")
        if not lp or not kind:
            return None
        return (lp, kind)

    async def get_token_account_balance(self, token_account: str) -> Optional[float]:
        """
        Return ui-amount of an SPL token account.

        Same processed+retry logic as decode_pool: vault accounts are often
        created in the same slot as the pool itself, so a `confirmed` query
        can hit "could not find account" right after a pool init.
        """
        import asyncio
        for delay in (0.0, 0.4, 1.0, 2.0):
            if delay:
                await asyncio.sleep(delay)
            result = await self._rpc(
                "getTokenAccountBalance",
                [token_account, {"commitment": "processed"}],
            )
            if not result:
                continue
            value = result.get("value", {})
            ui = value.get("uiAmount")
            try:
                return float(ui) if ui is not None else None
            except Exception:
                return None
        return None

    async def get_pool_liquidity_usd(
        self,
        pool_address: str,
        sol_price_usd: float,
        decoded: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        """
        Compute SOL-side liquidity * 2 as a proxy for total USD liquidity, and
        derive the meme-side USD price from the pool reserves.

        For a balanced AMM:
          - liquidity_usd = sol_balance × sol_price_usd × 2
          - meme price    = (sol_balance × sol_price_usd) / meme_balance

        The meme price is exactly what an instantaneous infinitesimal swap
        would yield, before fees and slippage. It's the most accurate
        snapshot of the pool's spot price.

        Pass `decoded` if you've already called decode_pool() to save 1 RPC.

        Returns:
          {
            "liquidity_usd": float,
            "meme_price_usd": float | None,
            "sol_balance": float,
            "meme_balance": float | None,
            "sol_side": "base" | "quote",
            "pool_kind": str,
          }
        Or None if the pool layout is unsupported / SOL leg not found / vault
        unreadable.
        """
        info = decoded if decoded is not None else await self.decode_pool(pool_address)
        if not info:
            return None
        sol_vault = info.get("sol_vault")
        if not sol_vault:
            # Pool doesn't have a SOL leg (USDC/USDT pair, etc.)
            return None
        sol_balance = await self.get_token_account_balance(sol_vault)
        if sol_balance is None or sol_balance <= 0:
            return None
        liquidity_usd = sol_balance * sol_price_usd * 2.0

        # --- Derive the meme price from the *other* vault ---
        # Pick the vault that ISN'T the SOL one. We know which side is SOL
        # from `sol_side`; the other side holds the meme.
        sol_side = info.get("sol_side")
        if sol_side == "base":
            meme_vault = info.get("quote_vault")
        elif sol_side == "quote":
            meme_vault = info.get("base_vault")
        else:
            meme_vault = None

        meme_balance: Optional[float] = None
        meme_price_usd: Optional[float] = None
        if meme_vault:
            meme_balance = await self.get_token_account_balance(meme_vault)
            if meme_balance and meme_balance > 0:
                # USD value of the SOL leg / meme tokens in pool = USD/meme
                meme_price_usd = (sol_balance * sol_price_usd) / meme_balance

        return {
            "liquidity_usd": liquidity_usd,
            "meme_price_usd": meme_price_usd,
            "sol_balance": sol_balance,
            "meme_balance": meme_balance,
            "sol_side": sol_side,
            "pool_kind": info.get("pool_kind"),
        }

    async def analyze_lp_status(
        self,
        pool_address: str,
        top_n: int = 5,
        decoded: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """
        Inspect LP token holders to decide if the LP is burnt or locked.

        Pass `decoded` if you've already called decode_pool() to save 1 RPC.

        Returns a dict:
          {
            "lp_mint": str | None,
            "pool_kind": str | None,
            "supply": float | None,
            "burnt_or_locked": True | False | None,
            "burn_pct": float,        # fraction of LP supply at burn addresses
            "lock_pct": float,        # fraction at known locker programs
            "top_holders": [ {address, owner, program_owner, ui_amount, pct, tag} ],
            "details": {...},
          }

        Decision rule:
          - burn_pct + lock_pct >= 0.95  -> True
          - top non-LP-program user holder >= 0.50 of supply -> False (creator still holds)
          - otherwise -> None (unknown)
        """
        out: dict[str, Any] = {
            "lp_mint": None,
            "pool_kind": None,
            "supply": None,
            "burnt_or_locked": None,
            "burn_pct": 0.0,
            "lock_pct": 0.0,
            "top_holders": [],
            "details": {},
        }

        if not pool_address:
            out["details"]["error"] = "no_pool_address"
            return out

        info = decoded if decoded is not None else await self.decode_pool(pool_address)
        if not info:
            out["details"]["error"] = "pool_layout_unsupported_or_unreadable"
            return out

        lp_mint = info.get("lp_mint")
        kind = info.get("pool_kind")
        if not lp_mint or not kind:
            out["details"]["error"] = "no_lp_mint_in_pool"
            return out
        out["lp_mint"] = lp_mint
        out["pool_kind"] = kind

        supply = await self.get_token_supply(lp_mint)
        out["supply"] = supply

        # Edge case: supply is 0 -> truly burnt (closed mint)
        if supply is not None and supply <= 0:
            out["burnt_or_locked"] = True
            out["burn_pct"] = 1.0
            out["details"]["reason"] = "lp_supply_zero"
            return out
        if supply is None:
            out["details"]["error"] = "no_lp_supply"
            return out

        largest = await self.get_largest_accounts(lp_mint)
        if not largest:
            out["details"]["error"] = "no_largest_lp_accounts"
            return out

        burn_amount = 0.0
        lock_amount = 0.0
        biggest_user_amount = 0.0
        rows: list[dict[str, Any]] = []

        for entry in largest[:top_n]:
            token_account = entry.get("address")
            ui = entry.get("uiAmount")
            try:
                amount = float(ui) if ui is not None else 0.0
            except Exception:
                amount = 0.0
            if not token_account or amount <= 0:
                continue

            wallet = await self.get_account_owner(token_account)
            program_owner = None
            tag = "user"

            if wallet in KNOWN_BURN_ADDRESSES:
                tag = "burn"
                burn_amount += amount
            elif wallet:
                # Check whether the wallet is itself a PDA owned by a locker
                # program (Streamflow, Meteora Lock, etc.) OR by a known AMM
                # protocol (PumpSwap, Raydium, Orca). In both cases the LP is
                # controlled by an immutable on-chain contract, not a human
                # keypair — treat it as locked.
                #
                # This is the critical fix for PumpSwap graduated pools:
                # when Pump.fun migrates a token to PumpSwap, the LP tokens
                # are either burned (supply → 0, caught above) or held by the
                # PumpSwap program PDA itself. Without this branch those
                # protocol-held tokens fell into the "user" bucket, making the
                # combined burn+lock percentage too low and returning None.
                program_owner = await self.get_account_program_owner(wallet)
                if program_owner in KNOWN_LOCKER_PROGRAMS:
                    tag = f"locker:{program_owner[:8]}"
                    lock_amount += amount
                elif program_owner in KNOWN_LP_PROGRAM_OWNERS:
                    tag = f"protocol_locked:{program_owner[:8]}"
                    lock_amount += amount
                else:
                    biggest_user_amount = max(biggest_user_amount, amount)

            rows.append({
                "address": token_account,
                "owner": wallet,
                "program_owner": program_owner,
                "ui_amount": amount,
                "pct": (amount / supply) if supply else None,
                "tag": tag,
            })

        out["top_holders"] = rows
        out["burn_pct"] = burn_amount / supply if supply else 0.0
        out["lock_pct"] = lock_amount / supply if supply else 0.0

        combined = out["burn_pct"] + out["lock_pct"]
        biggest_user_pct = (biggest_user_amount / supply) if supply else 0.0

        if combined >= 0.95:
            out["burnt_or_locked"] = True
            out["details"]["reason"] = f"burn+lock={combined:.2%}"
        elif biggest_user_pct >= 0.50:
            out["burnt_or_locked"] = False
            out["details"]["reason"] = f"user_holds_{biggest_user_pct:.2%}"
        else:
            out["burnt_or_locked"] = None
            out["details"]["reason"] = (
                f"inconclusive burn+lock={combined:.2%} "
                f"top_user={biggest_user_pct:.2%}"
            )

        return out


async def analyze_concentration_and_clusters(
    client: HeliusHoldersClient,
    mint: str,
    holders_to_inspect: int,
    max_top_holder_pct: float,
    run_concentration: bool = True,
    run_cluster: bool = True,
) -> dict[str, Any]:
    """
    Run concentration and/or cluster checks.

    The two checks are split because the cluster check is much more expensive
    (one getSignaturesForAddress + one getTransaction per top holder) and
    callers may want to run only one of them.

    When run_concentration is False, the function still resolves top holders
    (cheap-ish: needed to know whose funding sources to inspect) but does not
    set top_holder_pct.

    Returns:
      {
        "top_holder_pct": float | None,
        "cluster_detected": bool | None,
        "top_funders": [(funder, count), ...],
        "details": {...},
      }
    """
    out: dict[str, Any] = {
        "top_holder_pct": None,
        "cluster_detected": None,
        "top_funders": [],
        "details": {},
    }

    if not (run_concentration or run_cluster):
        return out

    supply = await client.get_token_supply(mint)
    if not supply or supply <= 0:
        out["details"]["error"] = "no_supply"
        return out

    largest = await client.get_largest_accounts(mint)
    if not largest:
        out["details"]["error"] = "no_largest_accounts"
        return out

    # For each large *token* account, resolve the wallet behind it and skip
    # known LP/burn/program owners.
    user_holders: list[tuple[str, float]] = []
    skipped: list[tuple[str, float, str]] = []

    for entry in largest[: max(holders_to_inspect, 10)]:
        token_account = entry.get("address")
        ui = entry.get("uiAmount")
        try:
            amount = float(ui) if ui is not None else 0.0
        except Exception:
            amount = 0.0
        if not token_account or amount <= 0:
            continue

        # Who owns this token account?
        wallet = await client.get_account_owner(token_account)

        # Determine the *program* owning whichever level we have:
        # - If we know the wallet (typical case): check whether THAT wallet
        #   is itself a PDA owned by an AMM program (e.g. a PumpSwap or
        #   Raydium pool's own state PDA, which holds vaults of itself).
        #   This is the case that was being missed for PumpSwap.
        # - If we don't know the wallet (token account not parseable):
        #   fall back to inspecting the token account's program owner.
        program_owner: Optional[str] = None
        if wallet:
            program_owner = await client.get_account_program_owner(wallet)
        else:
            program_owner = await client.get_account_program_owner(token_account)

        if program_owner in KNOWN_LP_PROGRAM_OWNERS:
            skipped.append((token_account, amount, f"lp:{program_owner}"))
            continue
        if wallet in KNOWN_NON_USER_OWNERS:
            skipped.append((token_account, amount, "burn"))
            continue
        if wallet is None:
            # Unknown - keep but mark
            skipped.append((token_account, amount, "unknown_owner"))
            continue

        user_holders.append((wallet, amount))

    if not user_holders:
        out["details"]["error"] = "no_user_holders_resolved"
        return out

    user_holders.sort(key=lambda x: x[1], reverse=True)

    if run_concentration:
        top_wallet, top_amount = user_holders[0]
        top_pct = top_amount / supply
        out["top_holder_pct"] = top_pct
        out["details"]["top_wallet"] = top_wallet
        out["details"]["skipped"] = skipped[:10]

    if run_cluster:
        # Cluster detection: look at top N holders' funding sources
        top_n = user_holders[: min(holders_to_inspect, len(user_holders))]
        funders: list[str] = []
        for wallet, _ in top_n:
            src = await client.get_funding_source(wallet)
            if src:
                funders.append(src[0])

        counter = Counter(funders)
        top_funders = counter.most_common(5)
        out["top_funders"] = top_funders

        # Cluster threshold: same funder seeded >=3 of the top holders
        cluster = any(count >= 3 for _, count in top_funders)
        out["cluster_detected"] = cluster
        out["details"]["funder_count"] = dict(counter)

    return out
