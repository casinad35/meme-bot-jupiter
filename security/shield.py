"""
The Shield: aggregates security checks before any buy.

Execution model
---------------
Sequential and short-circuiting: as soon as a critical check FAILS we stop
and return the report. Steps are ordered cheapest-first so we burn the least
RPC quota when rejecting (which is the common case for new memes):

  1. Cache lookup                        (0 RPC)
  2. On-chain mint authorities           (1 RPC) — also confirms the mint exists
  3. GoPlus security flags               (HTTP, no RPC)
  4. On-chain liquidity from pool vaults (2 RPC) + Jupiter fallback (HTTP)
  5. LP burnt or locked                  (~7 RPC, reuses decoded pool)
  6. Top-holder concentration            (~21 RPC)
  7. Cluster / funding-graph check       (~30 RPC, opt-in)

A token must pass *all* of these to be eligible.

Caching
-------
Verdicts (pass / fail) are cached per mint for `settings.shield_cache_seconds`.
A single mint frequently appears in multiple pools within a short window
(Raydium V4 + CPMM + Orca migration, etc.) — caching prevents 5x duplicate
shield runs.
"""
from __future__ import annotations

import time
from typing import Any, Optional

from config import settings
from models import SecurityReport, TokenInfo
from security.jupiter import JupiterClient
from security.goplus import GoPlusClient, parse_goplus_flags
from security.helius_holders import (
    HeliusHoldersClient,
    TOKEN_2022_PROGRAM,
    TRUSTED_AUTHORITY_PROGRAMS,
    analyze_concentration_and_clusters,
)
from utils.logger import logger


# Wrapped SOL mint — used to price the SOL leg of a pool for liquidity.
WSOL_MINT = "So11111111111111111111111111111111111111112"


class Shield:
    def __init__(
        self,
        goplus: GoPlusClient,
        jupiter: JupiterClient,
        holders: HeliusHoldersClient,
    ):
        self.goplus = goplus
        self.jupiter = jupiter
        self.holders = holders
        # mint -> (timestamp, finalized SecurityReport)
        self._verdict_cache: dict[str, tuple[float, SecurityReport]] = {}

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    async def evaluate(self, token: TokenInfo) -> SecurityReport:
        # --- Step 1: cache ---
        cached = self._cache_get(token.mint)
        if cached is not None:
            logger.info(
                f"[shield] CACHE {('PASS' if cached.passed else 'REJECT')} "
                f"{token.symbol} ({token.mint[:8]}...)"
            )
            return cached

        report = SecurityReport(token_mint=token.mint)
        logger.info(f"[shield] Evaluating {token.symbol} ({token.mint[:8]}...)")

        # Each step returns False if the report has hard-failed and we should
        # stop. Steps mutate `report` in place.
        decoded_pool: Optional[dict[str, Any]] = None

        if not await self._step_authorities(report, token):
            return self._finalize_and_cache(report, token)

        if not await self._step_goplus(report, token):
            return self._finalize_and_cache(report, token)

        decoded_pool = await self._step_liquidity(report, token)
        if not report.passed_far():  # any fails so far -> stop
            return self._finalize_and_cache(report, token)

        if not await self._step_lp_status(report, token, decoded_pool):
            return self._finalize_and_cache(report, token)

        if not await self._step_concentration(report, token):
            return self._finalize_and_cache(report, token)

        if settings.enable_cluster_check:
            await self._step_cluster(report, token)

        return self._finalize_and_cache(report, token)

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------
    def _cache_get(self, mint: str) -> Optional[SecurityReport]:
        ttl = float(settings.shield_cache_seconds or 0)
        if ttl <= 0:
            return None
        entry = self._verdict_cache.get(mint)
        if entry is None:
            return None
        ts, report = entry
        if (time.time() - ts) > ttl:
            self._verdict_cache.pop(mint, None)
            return None
        return report

    def _cache_put(self, mint: str, report: SecurityReport) -> None:
        ttl = float(settings.shield_cache_seconds or 0)
        if ttl <= 0:
            return
        if len(self._verdict_cache) > 2048:
            self._verdict_cache.clear()
        self._verdict_cache[mint] = (time.time(), report)

    def _finalize_and_cache(
        self, report: SecurityReport, token: TokenInfo
    ) -> SecurityReport:
        report.finalize()
        if report.passed:
            logger.success(
                f"[shield] PASS {token.symbol} top_holder={report.top_holder_pct} "
                f"liq=${report.liquidity_usd}"
            )
        else:
            logger.warning(
                f"[shield] REJECT {token.symbol} reasons={report.failures} "
                f"warnings={report.warnings}"
            )
        self._cache_put(token.mint, report)
        return report

    # ------------------------------------------------------------------
    # Step 1/cache handled in evaluate()
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Step 2: on-chain mint authorities (1 RPC, also confirms mint exists)
    #
    # We treat as "effectively renounced":
    #   * authority is null (true renouncement)
    #   * authority is a PDA owned by Pump.fun / PumpSwap / Raydium (the
    #     mint is controlled by a published smart contract whose supply is
    #     bounded by protocol rules, not by an operator keypair).
    # ------------------------------------------------------------------
    async def _step_authorities(
        self, report: SecurityReport, token: TokenInfo
    ) -> bool:
        info = await self.holders.get_mint_info(token.mint)
        if info is None:
            # get_mint_info has its own retry+logging; treat as hard fail.
            report.add_fail("cannot_read_mint_account")
            return False

        report.raw["mint_info"] = info
        mint_auth = info.get("mintAuthority")
        freeze_auth = info.get("freezeAuthority")

        # True renouncement = authority is None / "".
        mint_renounced_strict = mint_auth in (None, "")
        freeze_renounced_strict = freeze_auth in (None, "")

        # Check whether non-null authorities are owned by a trusted protocol
        # program. We only do the extra RPC if needed (saves quota when
        # authority is already null).
        mint_auth_owner: Optional[str] = None
        freeze_auth_owner: Optional[str] = None
        if not mint_renounced_strict and mint_auth:
            mint_auth_owner = await self.holders.get_account_program_owner(mint_auth)
        if not freeze_renounced_strict and freeze_auth:
            # Most of the time freeze_auth == mint_auth (Pump.fun sets both
            # to the same PDA), so reuse the lookup when possible.
            if freeze_auth == mint_auth:
                freeze_auth_owner = mint_auth_owner
            else:
                freeze_auth_owner = await self.holders.get_account_program_owner(freeze_auth)

        mint_trusted = mint_auth_owner in TRUSTED_AUTHORITY_PROGRAMS
        freeze_trusted = freeze_auth_owner in TRUSTED_AUTHORITY_PROGRAMS

        report.mint_authority_renounced = mint_renounced_strict or mint_trusted
        report.freeze_authority_renounced = freeze_renounced_strict or freeze_trusted

        if mint_trusted:
            report.add_warn("mint_authority_program_pda")
            report.raw["mint_authority_program_owner"] = mint_auth_owner
        if freeze_trusted:
            report.add_warn("freeze_authority_program_pda")
            report.raw["freeze_authority_program_owner"] = freeze_auth_owner

        if not report.mint_authority_renounced:
            report.add_fail(f"mint_authority_active:{mint_auth}")
        if not report.freeze_authority_renounced:
            report.add_fail(f"freeze_authority_active:{freeze_auth}")

        return report.passed_far()

    # ------------------------------------------------------------------
    # Step 3: GoPlus (HTTP, no RPC)
    # ------------------------------------------------------------------
    async def _step_goplus(self, report: SecurityReport, token: TokenInfo) -> bool:
        data = await self.goplus.solana_token_security(token.mint)
        if data is None:
            report.add_warn("goplus_unavailable")
            return True

        flags = parse_goplus_flags(data)
        report.raw["goplus"] = flags

        if flags.get("non_transferable") is True:
            report.is_honeypot = True
            report.add_fail("non_transferable_honeypot")
        elif flags.get("non_transferable") is False:
            report.is_honeypot = False

        if flags.get("freezable") is True:
            report.add_fail("freezable_authority_can_block_sells")

        tf = flags.get("transfer_fee_pct")
        if isinstance(tf, (int, float)) and tf >= 10:
            report.add_fail(f"abusive_transfer_fee_{tf}pct")

        if flags.get("mintable") is True:
            report.add_warn("mint_authority_active")
        if flags.get("balance_mutable_authority") is True:
            report.add_warn("balance_mutable_authority_active")
        if flags.get("transfer_hook_upgradable") is True:
            report.add_warn("transfer_hook_upgradable")

        return report.passed_far()

    # ------------------------------------------------------------------
    # Step 4: liquidity (on-chain primary, Jupiter fallback)
    # ------------------------------------------------------------------
    async def _step_liquidity(
        self, report: SecurityReport, token: TokenInfo
    ) -> Optional[dict[str, Any]]:
        """
        Returns the decoded pool dict (so step 5 can reuse it) or None.

        Liquidity sourcing priority:
          1. On-chain: decode pool layout, read SOL vault balance, multiply by
             SOL price * 2. Works the moment the pool exists, no indexer lag.
          2. Jupiter Tokens API V2 (HTTP). Only indexes after some traction.
          3. Whatever the monitor put in token.liquidity_usd.
        """
        liquidity: Optional[float] = None
        source: str = "unknown"
        decoded: Optional[dict[str, Any]] = None

        # --- Priority 1: on-chain ---
        if token.pool_address:
            try:
                decoded = await self.holders.decode_pool(token.pool_address)
            except Exception as e:
                logger.debug(f"[shield] decode_pool failed: {e}")
                decoded = None

            if decoded is not None and decoded.get("sol_vault"):
                # Need SOL price. Jupiter Price V3 is a single HTTP call.
                sol_price = await self.jupiter.price(WSOL_MINT)
                if sol_price and sol_price > 0:
                    try:
                        liq_info = await self.holders.get_pool_liquidity_usd(
                            token.pool_address, sol_price, decoded=decoded
                        )
                    except Exception as e:
                        logger.debug(f"[shield] on-chain liquidity failed: {e}")
                        liq_info = None
                    if liq_info:
                        liquidity = float(liq_info["liquidity_usd"])
                        source = "on_chain"
                        report.raw["liquidity_source_details"] = liq_info
                        # Also stamp the on-chain spot price onto the token
                        # so PaperTrader can use it without a Jupiter roundtrip.
                        meme_price = liq_info.get("meme_price_usd")
                        if meme_price and meme_price > 0:
                            token.price_usd = float(meme_price)
                            report.raw["meme_price_usd"] = meme_price
                        # Stamp vault info so the price feed can re-read the
                        # spot price every tick directly from the pool, with
                        # no Jupiter call needed.
                        token.sol_vault = decoded.get("sol_vault") or ""
                        token.pool_kind = decoded.get("pool_kind") or ""
                        # Identify the meme vault (the one that isn't SOL).
                        sol_side = decoded.get("sol_side")
                        if sol_side == "base":
                            token.meme_vault = decoded.get("quote_vault") or ""
                        elif sol_side == "quote":
                            token.meme_vault = decoded.get("base_vault") or ""

        # --- Priority 2: Jupiter overview ---
        overview = None
        if liquidity is None:
            overview = await self.jupiter.token_overview(token.mint)
            if overview:
                v = overview.get("liquidity")
                try:
                    v = float(v) if v is not None else None
                except Exception:
                    v = None
                if v is not None and v > 0:
                    liquidity = v
                    source = "jupiter"

        # --- Priority 3: monitor hint ---
        if liquidity is None and token.liquidity_usd > 0:
            liquidity = float(token.liquidity_usd)
            source = "monitor_hint"

        # Capture Jupiter audit data while we have it (cheap; one HTTP).
        # We intentionally do NOT call this if we already failed liquidity,
        # to keep the early-reject path fast.
        if liquidity is not None:
            security = await self.jupiter.token_security(token.mint)
            if security:
                report.raw["jupiter_security"] = {
                    k: security.get(k)
                    for k in (
                        "lockInfo", "freezeable", "freezeAuthority",
                        "mintAuthority", "ownerPercentage", "creatorPercentage",
                        "top10HolderPercent", "lpBurned", "isTrueToken",
                        "transferFeeEnable", "organicScore", "organicScoreLabel",
                    )
                }
                t10 = security.get("top10HolderPercent")
                try:
                    t10 = float(t10) if t10 is not None else None
                except Exception:
                    t10 = None
                if t10 is not None and t10 > 0.5:
                    report.add_warn(f"jupiter_top10_pct_{t10:.2f}")
                if security.get("isTrueToken") is False:
                    report.add_warn("token_not_verified_on_jupiter")

        report.liquidity_usd = liquidity
        report.raw["liquidity_source"] = source

        if liquidity is None:
            report.add_fail("liquidity_unknown")
        elif liquidity < settings.min_liquidity_usd:
            report.add_fail(f"liquidity_below_min_{liquidity:.0f}")
        elif liquidity > settings.max_liquidity_usd:
            report.add_fail(f"liquidity_above_max_{liquidity:.0f}")

        return decoded

    # ------------------------------------------------------------------
    # Step 5: LP burnt or locked (reuses decoded pool)
    # ------------------------------------------------------------------
    async def _step_lp_status(
        self,
        report: SecurityReport,
        token: TokenInfo,
        decoded_pool: Optional[dict[str, Any]],
    ) -> bool:
        lp_burnt_or_locked: Optional[bool] = None

        if not token.pool_address:
            report.raw["lp_status"] = {"error": "no_pool_address"}
        elif decoded_pool is None:
            # decode_pool already failed in step 4 -> unsupported layout
            report.raw["lp_status"] = {"error": "pool_layout_unsupported_or_unreadable"}
        else:
            try:
                lp_status = await self.holders.analyze_lp_status(
                    token.pool_address, decoded=decoded_pool
                )
                report.raw["lp_status"] = {
                    "lp_mint": lp_status.get("lp_mint"),
                    "pool_kind": lp_status.get("pool_kind"),
                    "supply": lp_status.get("supply"),
                    "burn_pct": lp_status.get("burn_pct"),
                    "lock_pct": lp_status.get("lock_pct"),
                    "burnt_or_locked": lp_status.get("burnt_or_locked"),
                    "reason": lp_status.get("details", {}).get("reason"),
                    "error": lp_status.get("details", {}).get("error"),
                }
                lp_burnt_or_locked = lp_status.get("burnt_or_locked")
            except Exception as e:
                logger.debug(f"[shield] LP status check failed: {e}")
                report.raw["lp_status"] = {"error": f"exception:{e}"}

        report.lp_locked_or_burnt = lp_burnt_or_locked
        if lp_burnt_or_locked is False:
            report.add_fail("lp_unlocked")
        elif lp_burnt_or_locked is None:
            # When the lock status can't be proven, treat that as a fail by
            # default. An unverified lock is what allowed the WorldCup rug:
            # shield PASS at t=0 with `lp_lock_status_unknown` warning, pool
            # drained at t=27min, position became unsellable. Users who
            # accept that risk can flip require_lp_lock_proof=False.
            #
            # Exception — PumpSwap graduated pools:
            # When Pump.fun migrates a token the LP is either burned outright
            # (supply → 0, caught above as True) or held by the PumpSwap
            # program PDA, which the helius_holders fix now counts as
            # "protocol_locked". If the status is *still* None for a PumpSwap
            # pool (e.g. fresh pool not yet indexed, RPC timing), it's far
            # safer to downgrade to a warning rather than reject — the
            # protocol design makes a classic LP-rug structurally impossible
            # on those pools (the AMM program controls the liquidity, not the
            # dev's hot wallet).
            pool_kind = (report.raw.get("lp_status") or {}).get("pool_kind", "")
            is_pumpswap = pool_kind == "pumpswap"
            if settings.require_lp_lock_proof and not is_pumpswap:
                report.add_fail("lp_lock_status_unknown")
            else:
                report.add_warn("lp_lock_status_unknown")

        return report.passed_far()

    # ------------------------------------------------------------------
    # Step 6: top-holder concentration (~21 RPC)
    # ------------------------------------------------------------------
    async def _step_concentration(
        self, report: SecurityReport, token: TokenInfo
    ) -> bool:
        mint_info = report.raw.get("mint_info") or {}
        token_program = mint_info.get("_program_owner")
        is_token_2022 = token_program == TOKEN_2022_PROGRAM

        # Short-circuit for Token-2022 mints: getTokenLargestAccounts on
        # standard Solana RPC always returns -32602 for them, and our
        # holder-walk would burn ~20 RPC chasing data that doesn't exist.
        # Use Jupiter's top10HolderPercent directly when available.
        if is_token_2022:
            jupiter_security = report.raw.get("jupiter_security") or {}
            jupiter_top10 = jupiter_security.get("top10HolderPercent")
            if jupiter_top10 is not None:
                top10_limit = settings.max_top_holder_pct * 3.0
                report.top_holder_pct = jupiter_top10
                report.raw["concentration_source"] = "jupiter_top10_fallback"
                if jupiter_top10 > top10_limit:
                    report.add_fail(
                        f"jupiter_top10_owns_{jupiter_top10:.2%}_exceeds_{top10_limit:.2%}"
                    )
                else:
                    report.add_warn(
                        f"using_jupiter_top10_for_token2022:{jupiter_top10:.2%}"
                    )
                return report.passed_far()
            # No Jupiter data either → genuinely unknown
            report.add_fail("top_holder_unknown:token_2022_no_jupiter_data")
            return report.passed_far()

        try:
            res = await analyze_concentration_and_clusters(
                self.holders,
                token.mint,
                holders_to_inspect=settings.holders_to_inspect,
                max_top_holder_pct=settings.max_top_holder_pct,
                # Cluster step disabled here; runs separately in step 7 if enabled
                run_cluster=False,
            )
        except Exception as e:
            logger.exception(f"holders analysis failed: {e}")
            report.add_warn("holders_analysis_failed")
            return True  # warning, not fatal

        report.raw["holders"] = res
        top_pct = res.get("top_holder_pct")

        if top_pct is None:
            # Classic SPL token but somehow concentration unresolved
            # (RPC errors, no user holders found, etc). Fail rather than
            # buy blind. Jupiter top10 may still help here if available.
            jupiter_security = report.raw.get("jupiter_security") or {}
            jupiter_top10 = jupiter_security.get("top10HolderPercent")
            err = (res.get("details") or {}).get("error", "no_top_holder_pct")
            if jupiter_top10 is not None:
                top10_limit = settings.max_top_holder_pct * 3.0
                report.top_holder_pct = jupiter_top10
                report.raw["concentration_source"] = "jupiter_top10_fallback"
                if jupiter_top10 > top10_limit:
                    report.add_fail(
                        f"jupiter_top10_owns_{jupiter_top10:.2%}_exceeds_{top10_limit:.2%}"
                    )
                else:
                    report.add_warn(
                        f"using_jupiter_top10_fallback:{jupiter_top10:.2%}"
                    )
            else:
                report.add_fail(f"top_holder_unknown:{err}")
        else:
            report.top_holder_pct = top_pct
            if top_pct > settings.max_top_holder_pct:
                report.add_fail(f"top_holder_owns_{top_pct:.2%}")

        return report.passed_far()

    # ------------------------------------------------------------------
    # Step 7: cluster / funding-graph (opt-in, ~30 RPC)
    # ------------------------------------------------------------------
    async def _step_cluster(
        self, report: SecurityReport, token: TokenInfo
    ) -> None:
        try:
            res = await analyze_concentration_and_clusters(
                self.holders,
                token.mint,
                holders_to_inspect=settings.holders_to_inspect,
                max_top_holder_pct=settings.max_top_holder_pct,
                run_cluster=True,
                run_concentration=False,  # already done in step 6
            )
        except Exception as e:
            logger.exception(f"cluster analysis failed: {e}")
            report.add_warn("cluster_analysis_failed")
            return

        cluster = res.get("cluster_detected")
        report.cluster_detected = cluster
        # Merge new info into the holders raw block without overwriting it
        existing = report.raw.get("holders") or {}
        existing["top_funders"] = res.get("top_funders", [])
        existing["cluster_detected"] = cluster
        report.raw["holders"] = existing

        if cluster is True:
            report.add_fail("funding_cluster_detected")
