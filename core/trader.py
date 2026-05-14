"""
Buy/sell execution layer.

Two implementations behind a common interface:

  * PaperTrader: simulated. Tracks balances in memory, uses Jupiter prices.
  * LiveTrader : real swaps via Jupiter Aggregator on Solana.

Public API:
    await trader.buy(mint, sol_amount)  -> (tokens_received, exec_price_usd)
    await trader.sell(mint, token_amount) -> (sol_received, exec_price_usd)

Both raise on failure; the caller decides what to do.
"""
from __future__ import annotations

import abc
import asyncio
import base64
import json
import os
from pathlib import Path
from typing import Optional

import base58
import httpx

from config import settings
from security.jupiter import JupiterClient
from utils.logger import logger


def _fmt_price(p: float) -> str:
    """Format a USD price compactly: enough digits to be informative even
    for very small meme prices ($1e-9 territory)."""
    if p <= 0:
        return "$0"
    if p >= 0.01:
        return f"${p:,.4f}"
    if p >= 1e-6:
        return f"${p:.8f}"
    # Very small: scientific to keep precision visible
    return f"${p:.4e}"


SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL = 1_000_000_000


class TraderError(Exception):
    pass


class BaseTrader(abc.ABC):
    @abc.abstractmethod
    async def buy(
        self,
        mint: str,
        sol_amount: float,
        hint_price_usd: Optional[float] = None,
    ) -> tuple[float, float]:
        """Spend `sol_amount` SOL on `mint`. Returns (tokens_received, exec_price_usd).

        `hint_price_usd` is an optional pre-computed USD price (e.g. derived
        from the pool's on-chain reserves). PaperTrader uses it directly when
        provided, avoiding a roundtrip to Jupiter's pricing endpoints which
        often miss freshly-graduated tokens. LiveTrader ignores it (real
        swaps quote on-chain anyway).
        """

    @abc.abstractmethod
    async def sell(
        self,
        mint: str,
        token_amount: float,
        hint_price_usd: Optional[float] = None,
    ) -> tuple[float, float]:
        """Sell `token_amount` of `mint` for SOL. Returns (sol_received, exec_price_usd)."""

    async def aclose(self) -> None:
        return None


# ============================================================================
# PAPER TRADER
# ============================================================================
class PaperTrader(BaseTrader):
    """
    Simulated trader. Uses Jupiter live price as fill price, applies a fixed
    slippage penalty so paper results don't look unrealistically clean.
    """

    SIMULATED_SLIPPAGE = 0.02  # 2% extra cost on each leg
    SOL_USD_FALLBACK = 150.0   # only used if Jupiter fails

    def __init__(self, jupiter: JupiterClient):
        self.jupiter = jupiter

    async def _sol_price_usd(self) -> float:
        p = await self.jupiter.price(SOL_MINT)
        return p if p else self.SOL_USD_FALLBACK

    async def _token_price_usd(
        self, mint: str, hint: Optional[float] = None
    ) -> float:
        """Return USD price per token. Prefer the caller-supplied hint
        (typically derived from on-chain pool reserves) before falling back
        to Jupiter's price endpoint, which is unreliable for fresh memes.
        """
        if hint is not None and hint > 0:
            return hint
        p = await self.jupiter.price(mint)
        if p is None or p <= 0:
            raise TraderError(f"no price for {mint}")
        return p

    async def buy(
        self,
        mint: str,
        sol_amount: float,
        hint_price_usd: Optional[float] = None,
    ) -> tuple[float, float]:
        sol_usd = await self._sol_price_usd()
        token_usd = await self._token_price_usd(mint, hint_price_usd)
        # Worse fill due to "slippage"
        effective_price = token_usd * (1 + self.SIMULATED_SLIPPAGE)
        usd_in = sol_amount * sol_usd
        tokens_received = usd_in / effective_price
        logger.info(
            f"[paper] BUY  {sol_amount} SOL -> {tokens_received:,.2f} tokens "
            f"@ {_fmt_price(effective_price)} (mint={mint[:8]}..)"
        )
        return tokens_received, effective_price

    async def sell(
        self,
        mint: str,
        token_amount: float,
        hint_price_usd: Optional[float] = None,
    ) -> tuple[float, float]:
        sol_usd = await self._sol_price_usd()
        token_usd = await self._token_price_usd(mint, hint_price_usd)
        effective_price = token_usd * (1 - self.SIMULATED_SLIPPAGE)
        usd_out = token_amount * effective_price
        sol_out = usd_out / sol_usd
        logger.info(
            f"[paper] SELL {token_amount:,.2f} tokens -> {sol_out:.4f} SOL "
            f"@ {_fmt_price(effective_price)} (mint={mint[:8]}..)"
        )
        return sol_out, effective_price


# ============================================================================
# LIVE TRADER (Jupiter Aggregator)
# ============================================================================
# When a JUPITER_API_KEY is configured, we use the authenticated Pro endpoints
# (api.jup.ag/swap/v1) for higher rate limits. Otherwise we use the free
# legacy v6 quote-api which doesn't require auth.
JUP_QUOTE_FREE = "https://quote-api.jup.ag/v6/quote"
JUP_SWAP_FREE  = "https://quote-api.jup.ag/v6/swap"
JUP_QUOTE_PRO  = "https://api.jup.ag/swap/v1/quote"
JUP_SWAP_PRO   = "https://api.jup.ag/swap/v1/swap"


def _load_keypair_secret() -> bytes:
    """Load secret key bytes from base58 string or keypair JSON file."""
    if settings.wallet_private_key_base58:
        try:
            secret = base58.b58decode(settings.wallet_private_key_base58)
        except Exception as e:
            raise TraderError(f"invalid base58 private key: {e}")
        if len(secret) not in (64, 32):
            raise TraderError(f"unexpected secret length {len(secret)}")
        return secret

    if settings.wallet_keypair_path and os.path.exists(settings.wallet_keypair_path):
        raw = Path(settings.wallet_keypair_path).read_text().strip()
        try:
            arr = json.loads(raw)
        except Exception as e:
            raise TraderError(f"keypair json parse failed: {e}")
        if not isinstance(arr, list) or len(arr) not in (64, 32):
            raise TraderError("keypair json must be a list of 64 (or 32) ints")
        return bytes(arr)

    raise TraderError("no wallet configured: set WALLET_PRIVATE_KEY_BASE58 or WALLET_KEYPAIR_PATH")


class LiveTrader(BaseTrader):
    """
    Real on-chain swaps via Jupiter + Helius RPC.

    Flow per swap:
      1. GET /quote with input/output mints, amount, slippage
      2. POST /swap with the quoteResponse + user public key -> base64 tx
      3. Sign the VersionedTransaction with the wallet
      4. Send via RPC sendTransaction (with retries baked in)
      5. Poll getSignatureStatuses until confirmed or timeout

    Notes:
      * We use SOL <-> token directly. Jupiter handles routing.
      * priorityFeeLamports is set from settings.
      * When JUPITER_API_KEY is configured we hit api.jup.ag/swap/v1 with the
        x-api-key header (higher rate limits). Otherwise we use the public
        quote-api.jup.ag/v6 endpoints.
      * On sell, we use a higher slippage tolerance because exits matter more
        than entries (a missed exit can cost everything).
    """

    # Slippage multipliers applied on top of the configured base slippage.
    # Sells need more room: when a meme is dumping you'd rather get out at
    # -20% than have the tx revert and lose -90% by the time you retry.
    SELL_SLIPPAGE_MULTIPLIER = 2.0
    SELL_SLIPPAGE_MAX_BPS = 5000  # hard cap at 50%

    def __init__(self, rpc_url: str, jupiter: JupiterClient, slippage_bps: int = 1500):
        # Local imports so paper-only setups don't need solders
        from solders.keypair import Keypair  # type: ignore
        from solana.rpc.async_api import AsyncClient  # type: ignore

        self._Keypair = Keypair
        self.rpc_url = rpc_url
        self.jupiter = jupiter
        self.slippage_bps = slippage_bps

        # Pick endpoint set + auth header based on whether a key is configured
        if jupiter.api_key:
            self._quote_url = JUP_QUOTE_PRO
            self._swap_url = JUP_SWAP_PRO
            self._jup_headers: dict[str, str] = {"x-api-key": jupiter.api_key}
            logger.info("[live] using Jupiter Pro endpoints (api.jup.ag/swap/v1)")
        else:
            self._quote_url = JUP_QUOTE_FREE
            self._swap_url = JUP_SWAP_FREE
            self._jup_headers = {}
            logger.info("[live] using Jupiter free endpoints (quote-api.jup.ag/v6)")

        secret = _load_keypair_secret()
        if len(secret) == 32:
            self.keypair = Keypair.from_seed(secret)
        else:
            self.keypair = Keypair.from_bytes(secret)
        self.pubkey = str(self.keypair.pubkey())
        logger.info(f"[live] wallet loaded: {self.pubkey}")

        self.rpc = AsyncClient(rpc_url, commitment="confirmed")
        self._http = httpx.AsyncClient(timeout=20.0)

        # Token decimal cache (mint -> int). Avoids re-fetching for repeated
        # partial sells of the same position.
        self._decimals_cache: dict[str, int] = {}

    async def aclose(self) -> None:
        await self._http.aclose()
        await self.rpc.close()

    async def _get_decimals(self, mint: str) -> int:
        """Fetch (and cache) a token's decimals. Defaults to 6 if unreachable."""
        if mint in self._decimals_cache:
            return self._decimals_cache[mint]
        try:
            from solders.pubkey import Pubkey  # type: ignore
            info = await self.rpc.get_token_supply(Pubkey.from_string(mint))
            if info and info.value:
                d = int(info.value.decimals)
                self._decimals_cache[mint] = d
                return d
        except Exception as e:
            logger.warning(f"[live] decimals fetch failed for {mint[:8]}..: {e}; defaulting to 6")
        self._decimals_cache[mint] = 6
        return 6

    async def _get_quote(self, in_mint: str, out_mint: str, amount: int, slippage_bps: int) -> dict:
        params = {
            "inputMint": in_mint,
            "outputMint": out_mint,
            "amount": str(amount),
            "slippageBps": str(slippage_bps),
            "onlyDirectRoutes": "false",
            "asLegacyTransaction": "false",
        }
        r = await self._http.get(self._quote_url, params=params, headers=self._jup_headers)
        if r.status_code >= 400:
            raise TraderError(
                f"jupiter quote {r.status_code}: {r.text[:200]}"
            )
        data = r.json()
        if "outAmount" not in data:
            raise TraderError(f"jupiter quote missing outAmount: {data}")
        return data

    async def _build_swap_tx(self, quote: dict) -> str:
        body = {
            "quoteResponse": quote,
            "userPublicKey": self.pubkey,
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": settings.priority_fee_microlamports,
        }
        r = await self._http.post(self._swap_url, json=body, headers=self._jup_headers)
        if r.status_code >= 400:
            raise TraderError(f"jupiter swap {r.status_code}: {r.text[:200]}")
        data = r.json()
        if "swapTransaction" not in data:
            raise TraderError(f"jupiter swap missing swapTransaction: {data}")
        return data["swapTransaction"]

    async def _send_signed(self, tx_b64: str, confirm_timeout_s: float = 60.0) -> str:
        """
        Decode, resign, send, and confirm a VersionedTransaction.
        Raises TraderError on any failure (including on-chain revert).
        """
        from solders.transaction import VersionedTransaction  # type: ignore
        from solana.rpc.types import TxOpts  # type: ignore

        raw = base64.b64decode(tx_b64)
        tx = VersionedTransaction.from_bytes(raw)
        # Resign with our keypair. solders' constructor takes (message, signers).
        signed = VersionedTransaction(tx.message, [self.keypair])

        opts = TxOpts(
            skip_preflight=True,           # faster send; we accept that some revert
            preflight_commitment="confirmed",
            max_retries=5,                  # let RPC node rebroadcast
        )
        try:
            resp = await self.rpc.send_raw_transaction(bytes(signed), opts=opts)
        except Exception as e:
            raise TraderError(f"send_raw_transaction failed: {e}")
        sig = str(resp.value)
        logger.info(f"[live] tx sent: {sig}")

        # Poll for confirmation. We check every 1.5s up to confirm_timeout_s.
        import time as _t
        deadline = _t.monotonic() + confirm_timeout_s
        while _t.monotonic() < deadline:
            await asyncio.sleep(1.5)
            try:
                status = await self.rpc.get_signature_statuses([resp.value])
            except Exception as e:
                logger.debug(f"[live] status poll error (will retry): {e}")
                continue
            val = status.value[0] if status and status.value else None
            if val and val.confirmation_status:
                if val.err:
                    raise TraderError(f"tx reverted on-chain: {val.err} sig={sig}")
                logger.success(f"[live] tx confirmed: {sig}")
                return sig
        raise TraderError(f"tx not confirmed within {confirm_timeout_s}s: {sig}")

    async def buy(
        self,
        mint: str,
        sol_amount: float,
        hint_price_usd: Optional[float] = None,
    ) -> tuple[float, float]:
        # hint_price_usd is unused here: live quotes always go through Jupiter.
        amount_lamports = int(sol_amount * LAMPORTS_PER_SOL)
        quote = await self._get_quote(SOL_MINT, mint, amount_lamports, self.slippage_bps)
        out_amount_raw = int(quote["outAmount"])

        decimals = await self._get_decimals(mint)
        tokens_received_ui = out_amount_raw / (10 ** decimals)
        if tokens_received_ui <= 0:
            raise TraderError(f"buy quote produced zero output for {mint}")

        sol_usd = await self._sol_usd()
        in_usd = sol_amount * sol_usd
        exec_price = in_usd / tokens_received_ui

        logger.info(
            f"[live] BUY  quote: {sol_amount} SOL -> {tokens_received_ui:,.2f} tokens "
            f"@ {_fmt_price(exec_price)} (slippage_bps={self.slippage_bps}, mint={mint[:8]}..)"
        )

        tx_b64 = await self._build_swap_tx(quote)
        await self._send_signed(tx_b64, confirm_timeout_s=45.0)

        return float(tokens_received_ui), float(exec_price)

    async def sell(
        self,
        mint: str,
        token_amount: float,
        hint_price_usd: Optional[float] = None,
    ) -> tuple[float, float]:
        # hint_price_usd unused: real swaps quote on-chain via Jupiter.
        decimals = await self._get_decimals(mint)
        amount_raw = int(token_amount * (10 ** decimals))
        if amount_raw <= 0:
            raise TraderError(f"sell amount rounds to zero: {token_amount} @ {decimals}d")

        # Sells use a wider slippage window: a missed exit can cost everything,
        # whereas a fail-fast on buy just means we skip the trade.
        sell_slippage_bps = min(
            int(self.slippage_bps * self.SELL_SLIPPAGE_MULTIPLIER),
            self.SELL_SLIPPAGE_MAX_BPS,
        )
        quote = await self._get_quote(mint, SOL_MINT, amount_raw, sell_slippage_bps)
        out_lamports = int(quote["outAmount"])
        sol_out = out_lamports / LAMPORTS_PER_SOL

        sol_usd = await self._sol_usd()
        usd_out = sol_out * sol_usd
        exec_price = usd_out / token_amount if token_amount else 0.0

        logger.info(
            f"[live] SELL quote: {token_amount:,.2f} tokens -> {sol_out:.4f} SOL "
            f"@ {_fmt_price(exec_price)} (slippage_bps={sell_slippage_bps}, mint={mint[:8]}..)"
        )

        tx_b64 = await self._build_swap_tx(quote)
        # Sells get a longer confirm window: we'd rather wait than retry into
        # worse market conditions.
        await self._send_signed(tx_b64, confirm_timeout_s=90.0)

        return float(sol_out), float(exec_price)

    async def _sol_usd(self) -> float:
        p = await self.jupiter.price(SOL_MINT)
        return p if p else 150.0


def make_trader(rpc_url: str, jupiter: JupiterClient) -> BaseTrader:
    if settings.trading_mode.value == "live":
        logger.warning("[trader] LIVE MODE - real funds at risk")
        return LiveTrader(rpc_url, jupiter, slippage_bps=settings.slippage_bps)
    logger.info("[trader] PAPER MODE - simulated trades")
    return PaperTrader(jupiter)
