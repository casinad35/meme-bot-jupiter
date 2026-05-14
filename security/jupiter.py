"""
Jupiter API client (Solana).

Replaces the previous Birdeye client. Two Jupiter APIs cover everything we
used Birdeye for:

  * Tokens API V2  (/tokens/v2/search?query=<mint>)
        -> liquidity, market cap, holder count, FDV, decimals, symbol, name,
           usdPrice, and an `audit` block (mintAuthorityDisabled,
           freezeAuthorityDisabled, topHoldersPercentage).
        -> Replaces both Birdeye's token_overview and token_security.
  * Price API V3   (/price/v3?ids=<mint1>,<mint2>,...)
        -> Fast batch price polling for open positions (up to 50 ids/call).

Auth:
  * If an API key is configured (from https://portal.jup.ag), we hit
    api.jup.ag (Pro tier rate limits). Without a key we fall back to
    lite-api.jup.ag (free, lower rate limits).
  * Header name is `x-api-key`.

Get a key:
  1. Go to https://portal.jup.ag/
  2. Create a project (e.g. "casinad") and add an API key.
  3. Put it in .env as JUPITER_API_KEY=...
"""
from __future__ import annotations

import time
from typing import Any, Optional

import httpx

from utils.logger import logger


JUP_LITE = "https://lite-api.jup.ag"   # free, no key needed
JUP_PRO  = "https://api.jup.ag"         # paid tier, x-api-key required

# Wrapped SOL mint
WSOL = "So11111111111111111111111111111111111111112"


class _RateLimitGate:
    """Global circuit breaker for Jupiter 429s.

    When we hit a 429, we exponentially back off all subsequent Jupiter
    calls until the cooldown expires. This prevents the bot from spamming
    hundreds of failed log lines per minute when Jupiter is rate-limiting.

    Backoff: 5s -> 10s -> 20s -> 40s -> capped at 120s. Reset on success.
    """
    def __init__(self):
        import time as _t
        self._t = _t
        self._until: float = 0.0
        self._streak: int = 0

    def is_open(self) -> bool:
        return self._t.monotonic() < self._until

    def remaining(self) -> float:
        return max(0.0, self._until - self._t.monotonic())

    def trip(self) -> None:
        self._streak += 1
        backoff = min(5.0 * (2 ** (self._streak - 1)), 120.0)
        self._until = self._t.monotonic() + backoff
        # Log only on streak == 1 or every 5th hit, to avoid log spam
        if self._streak == 1 or self._streak % 5 == 0:
            logger.warning(
                f"[jupiter] rate limited (streak={self._streak}); "
                f"pausing all Jupiter calls for {backoff:.0f}s"
            )

    def reset(self) -> None:
        if self._streak:
            logger.info("[jupiter] rate limit cleared, resuming")
        self._streak = 0
        self._until = 0.0

# Tiny TTL cache so token_overview() and token_security() called back-to-back
# on the same mint (which the shield does in parallel) only hit the API once.
_CACHE_TTL_SECONDS = 8.0


class JupiterClient:
    """Drop-in replacement for the old BirdeyeClient."""

    def __init__(self, api_key: Optional[str], timeout: float = 8.0):
        self.api_key = (api_key or "").strip() or None
        self.base = JUP_PRO if self.api_key else JUP_LITE
        self._client = httpx.AsyncClient(timeout=timeout)
        self._gate = _RateLimitGate()
        # mint -> (timestamp, raw token info dict)
        self._token_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        h = {"accept": "application/json"}
        if self.api_key:
            h["x-api-key"] = self.api_key
        return h

    async def _guarded_get(
        self, url: str, params: Optional[dict[str, Any]] = None
    ) -> Optional[dict[str, Any]]:
        """
        GET wrapper that respects the rate-limit gate. Returns parsed JSON
        or None on failure (gate-paused, network error, 429, or non-2xx).
        Trips the gate on 429s. The caller decides what to do with None.
        """
        if self._gate.is_open():
            return None
        try:
            r = await self._client.get(url, params=params, headers=self._headers())
        except httpx.HTTPError as e:
            logger.debug(f"Jupiter GET network error {url}: {e}")
            return None
        if r.status_code == 429:
            self._gate.trip()
            return None
        if r.status_code >= 400:
            # Don't trip the gate on 4xx other than 429 — those are usually
            # per-request errors (unsupported mint, bad params).
            logger.debug(f"Jupiter GET {url} returned {r.status_code}")
            return None
        try:
            payload = r.json()
        except Exception as e:
            logger.debug(f"Jupiter GET {url}: bad JSON: {e}")
            return None
        # Successful call: clear the gate state if it was tripped.
        self._gate.reset()
        return payload

    # ------------------------------------------------------------------
    # Internal: fetch /tokens/v2/search and cache the result briefly
    # ------------------------------------------------------------------
    async def _token_info(self, mint: str) -> Optional[dict[str, Any]]:
        # Cache hit?
        cached = self._token_cache.get(mint)
        if cached is not None:
            ts, info = cached
            if time.time() - ts < _CACHE_TTL_SECONDS:
                return info

        url = f"{self.base}/tokens/v2/search"
        payload = await self._guarded_get(url, params={"query": mint})
        if payload is None:
            return None

        # Response is a list of matching tokens. Pick the exact-mint match.
        if not isinstance(payload, list):
            return None
        match: Optional[dict[str, Any]] = None
        for item in payload:
            if isinstance(item, dict) and item.get("id") == mint:
                match = item
                break
        if match is None and payload:
            # No exact match, but we asked for a mint; do not guess.
            return None

        # Cap cache size
        if len(self._token_cache) > 1024:
            self._token_cache.clear()
        self._token_cache[mint] = (time.time(), match)
        return match

    # ------------------------------------------------------------------
    # Birdeye-compatible interface
    # ------------------------------------------------------------------
    async def token_overview(self, mint: str) -> Optional[dict[str, Any]]:
        """
        Returns a Birdeye-overview-shaped dict so callers don't need to change.
        Keys provided: symbol, name, decimals, price, liquidity, marketCap,
        holder, fdv.
        """
        info = await self._token_info(mint)
        if info is None:
            return None

        def _f(v: Any) -> Optional[float]:
            try:
                return float(v) if v is not None else None
            except Exception:
                return None

        return {
            "symbol": info.get("symbol"),
            "name": info.get("name"),
            "decimals": info.get("decimals"),
            "price": _f(info.get("usdPrice")),
            "liquidity": _f(info.get("liquidity")),
            "marketCap": _f(info.get("mcap")),
            "fdv": _f(info.get("fdv")),
            "holder": info.get("holderCount"),
            # passthroughs that may be useful elsewhere
            "isVerified": info.get("isVerified"),
            "organicScore": info.get("organicScore"),
            "organicScoreLabel": info.get("organicScoreLabel"),
            "tags": info.get("tags"),
            "firstPool": info.get("firstPool"),
        }

    async def token_security(self, mint: str) -> Optional[dict[str, Any]]:
        """
        Returns a Birdeye-security-shaped dict. Fields Jupiter doesn't expose
        (lpBurned, lockInfo, creatorPercentage, transferFeeEnable) come back
        as None and the shield treats those as "unknown -> warning".
        """
        info = await self._token_info(mint)
        if info is None:
            return None

        audit = info.get("audit") or {}

        # Jupiter returns topHoldersPercentage as a percentage (0-100).
        # Birdeye returned it as a fraction (0-1). Convert so downstream
        # comparisons against `settings.max_top_holder_pct` (a fraction)
        # keep working.
        top_pct_raw = audit.get("topHoldersPercentage")
        try:
            top_pct = float(top_pct_raw) / 100.0 if top_pct_raw is not None else None
        except Exception:
            top_pct = None

        mint_disabled = audit.get("mintAuthorityDisabled")
        freeze_disabled = audit.get("freezeAuthorityDisabled")

        return {
            # mirrors of Birdeye keys
            "lpBurned": None,                 # not exposed by Jupiter
            "lockInfo": None,                 # not exposed by Jupiter
            "freezeable": (not freeze_disabled) if freeze_disabled is not None else None,
            "freezeAuthority": None if freeze_disabled else "active",
            "mintAuthority": None if mint_disabled else "active",
            "ownerPercentage": None,
            "creatorPercentage": None,
            "top10HolderPercent": top_pct,
            "isTrueToken": info.get("isVerified"),
            "transferFeeEnable": None,
            # Jupiter-only extras (handy for reports/logging)
            "organicScore": info.get("organicScore"),
            "organicScoreLabel": info.get("organicScoreLabel"),
        }

    # ------------------------------------------------------------------
    # Price API V3
    # ------------------------------------------------------------------
    async def price(self, mint: str) -> Optional[float]:
        """
        Get current USD price for a single mint.

        Two-stage lookup:
          1. Price API V3 (`/price/v3?ids=...`) — fast batch endpoint, but
             only indexes tokens with some traction.
          2. Quote API (`/swap/v1/quote` or `/v6/quote`) — works for ANY
             token that has on-chain liquidity routable by Jupiter, including
             freshly-graduated PumpSwap pools. We quote 1 SOL -> token, then
             invert to USD/token using the SOL price.

        Stage 2 is used only when stage 1 misses, because it costs an extra
        HTTP round trip per call.
        """
        prices = await self.prices_multi([mint])
        if mint in prices and prices[mint] > 0:
            return prices[mint]
        # Fallback: Jupiter quote (works for any pool with liquidity)
        return await self._quote_price_usd(mint)

    async def _quote_price_usd(self, mint: str) -> Optional[float]:
        """
        Use Jupiter Quote API to derive a USD price for `mint`.

        We swap 0.01 SOL -> mint, read how many tokens come out, and invert.
        Returns None if Jupiter can't route it.
        """
        if mint == WSOL:
            # Trivial: SOL price comes from Price API V3, never falls here.
            return None

        # SOL price (in USD) — assumed already cacheable by Price API V3
        sol_prices = await self.prices_multi([WSOL])
        sol_usd = sol_prices.get(WSOL)
        if not sol_usd or sol_usd <= 0:
            return None

        # Quote API endpoint depends on whether we have a Pro key
        if self.api_key:
            url = "https://api.jup.ag/swap/v1/quote"
        else:
            url = "https://quote-api.jup.ag/v6/quote"

        # 0.01 SOL = 10_000_000 lamports — small enough to avoid impacting price
        params = {
            "inputMint": WSOL,
            "outputMint": mint,
            "amount": "10000000",
            "slippageBps": "1500",
            "onlyDirectRoutes": "false",
            "asLegacyTransaction": "false",
        }
        payload = await self._guarded_get(url, params=params)
        if payload is None:
            return None

        out_amount_raw = payload.get("outAmount")
        if not out_amount_raw:
            return None
        try:
            out_amount = int(out_amount_raw)
        except Exception:
            return None
        if out_amount <= 0:
            return None

        # We need the output token's decimals to get a real ui-amount.
        # The quote response includes it.
        decimals = None
        for key in ("outputDecimals", "outDecimals"):
            if key in payload:
                try:
                    decimals = int(payload[key])
                    break
                except Exception:
                    pass
        if decimals is None:
            # Fall back to fetching from Tokens API V2 (1 RPC, cached for 8s)
            info = await self._token_info(mint)
            if info and info.get("decimals") is not None:
                try:
                    decimals = int(info["decimals"])
                except Exception:
                    decimals = None
        if decimals is None:
            return None

        ui_out = out_amount / (10 ** decimals)
        if ui_out <= 0:
            return None
        # 0.01 SOL × sol_usd = USD spent
        usd_in = 0.01 * sol_usd
        # Price per token = USD spent / tokens received
        return usd_in / ui_out

    async def prices_multi(self, mints: list[str]) -> dict[str, float]:
        """
        Batch USD price fetch. Price API V3 accepts up to 50 ids per request,
        so we chunk if needed.
        """
        if not mints:
            return {}

        out: dict[str, float] = {}
        # Dedupe while preserving order
        seen: set[str] = set()
        ordered = [m for m in mints if not (m in seen or seen.add(m))]

        url = f"{self.base}/price/v3"
        for i in range(0, len(ordered), 50):
            chunk = ordered[i : i + 50]
            payload = await self._guarded_get(
                url, params={"ids": ",".join(chunk)}
            )
            if payload is None:
                continue

            if not isinstance(payload, dict):
                continue
            for mint, info in payload.items():
                if not isinstance(info, dict):
                    continue
                v = info.get("usdPrice")
                if v is None:
                    continue
                try:
                    out[mint] = float(v)
                except Exception:
                    continue
        return out
