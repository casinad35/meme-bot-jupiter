"""
GoPlus Security API client (Solana).

Docs: https://docs.gopluslabs.io/reference/api-overview-solana

Authentication
--------------
GoPlus has two authentication modes:

  1. **Anonymous**: no key, ~30 req/min. Just hit the endpoint.

  2. **Authenticated**: requires app_key + app_secret. The flow is:
     a. POST /api/v1/token with {app_key, time, sign} where
        sign = sha1(app_key + str(time) + app_secret).
     b. Server returns {access_token, expires_in}.
     c. Use the access_token as the Authorization header on subsequent
        calls.

GoPlus deprecated the simple "Authorization: <api_key>" pattern in favor of
this JWT flow. Sending a raw key in Authorization yields "signature
verification failure".

Configuration
-------------
GOPLUS_API_KEY in .env can be either:

  * `app_key:app_secret` (preferred) -> we run the full JWT flow and
    auto-refresh the access_token before it expires.
  * `<access_token>` (legacy) -> we send it directly as the Authorization
    header, no refresh. Will break when the token expires.
  * empty -> we hit the anonymous endpoint.

If authentication fails for any reason, we transparently fall back to the
anonymous endpoint so the bot keeps running.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any, Optional

import httpx

from utils.logger import logger


GOPLUS_BASE = "https://api.gopluslabs.io"
TOKEN_PATH = "/api/v1/token"
SOLANA_TOKEN_SECURITY_PATH = "/api/v1/solana/token_security"


class GoPlusClient:
    def __init__(self, api_key: Optional[str] = None, timeout: float = 8.0):
        # Parse the env value into (app_key, app_secret) or treat as bearer token
        self._app_key: Optional[str] = None
        self._app_secret: Optional[str] = None
        self._static_token: Optional[str] = None
        if api_key:
            api_key = api_key.strip()
            if ":" in api_key:
                parts = api_key.split(":", 1)
                self._app_key, self._app_secret = parts[0].strip(), parts[1].strip()
            else:
                # Legacy mode: treat as a pre-generated access token
                self._static_token = api_key

        # JWT cache
        self._access_token: Optional[str] = None
        self._access_token_expiry: float = 0.0
        self._refresh_lock = asyncio.Lock()
        # Once-per-process flag so we don't spam logs on repeated auth failures
        self._auth_failed_warned = False

        self._client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Internal: get a valid access token (cached, auto-refreshed)
    # ------------------------------------------------------------------
    async def _get_access_token(self) -> Optional[str]:
        """Return a valid access_token, refreshing if needed. None if auth fails."""
        if self._static_token:
            return self._static_token
        if not (self._app_key and self._app_secret):
            return None

        # Cached token still good (60s safety margin)?
        if self._access_token and time.time() < (self._access_token_expiry - 60):
            return self._access_token

        async with self._refresh_lock:
            # Re-check inside the lock in case another coroutine refreshed
            if self._access_token and time.time() < (self._access_token_expiry - 60):
                return self._access_token

            now_s = int(time.time())
            sign_str = f"{self._app_key}{now_s}{self._app_secret}"
            sign = hashlib.sha1(sign_str.encode("utf-8")).hexdigest()
            body = {"app_key": self._app_key, "time": now_s, "sign": sign}
            try:
                r = await self._client.post(GOPLUS_BASE + TOKEN_PATH, json=body)
                r.raise_for_status()
                payload = r.json()
            except httpx.HTTPError as e:
                if not self._auth_failed_warned:
                    logger.warning(
                        f"GoPlus token request failed: {e}. "
                        f"Falling back to anonymous endpoint."
                    )
                    self._auth_failed_warned = True
                return None

            if payload.get("code") != 1:
                if not self._auth_failed_warned:
                    logger.warning(
                        f"GoPlus token request rejected: {payload.get('message')}. "
                        f"Check that GOPLUS_API_KEY is 'app_key:app_secret'. "
                        f"Falling back to anonymous endpoint."
                    )
                    self._auth_failed_warned = True
                return None

            result = payload.get("result", {}) or {}
            self._access_token = result.get("access_token")
            expires_in = int(result.get("expires_in") or 7200)  # default 2h
            self._access_token_expiry = time.time() + expires_in
            logger.info(
                f"[goplus] access token acquired (expires in {expires_in}s)"
            )
            return self._access_token

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def solana_token_security(self, mint: str) -> Optional[dict[str, Any]]:
        """Returns the GoPlus security dict for the given mint, or None on failure."""
        url = GOPLUS_BASE + SOLANA_TOKEN_SECURITY_PATH
        params = {"contract_addresses": mint}

        # Try authenticated first if we have credentials, then anonymous
        # fallback. Some GoPlus endpoints expect "Bearer <token>" while
        # others want the raw token; we try both shapes before giving up.
        attempts: list[dict[str, str]] = []
        token = await self._get_access_token()
        if token:
            attempts.append({"Authorization": f"Bearer {token}"})
            attempts.append({"Authorization": token})
        attempts.append({})  # anonymous

        last_err: Optional[str] = None
        for headers in attempts:
            try:
                r = await self._client.get(url, params=params, headers=headers)
                r.raise_for_status()
                payload = r.json()
            except httpx.HTTPError as e:
                last_err = f"http:{e}"
                continue

            code = payload.get("code")
            if code != 1:
                last_err = f"code={code} msg={payload.get('message')}"
                # If auth failed specifically, drop the cached token and retry
                # anonymously; otherwise no point retrying.
                if "signature" in str(payload.get("message", "")).lower():
                    self._access_token = None
                    self._access_token_expiry = 0.0
                continue

            result = payload.get("result", {}) or {}
            if not isinstance(result, dict):
                last_err = "result_not_dict"
                continue
            for k, v in result.items():
                if k.lower() == mint.lower():
                    return v
            # Mint not present in response - usually means GoPlus has no data
            return None

        logger.warning(f"GoPlus unavailable for {mint}: {last_err}")
        return None


def parse_goplus_flags(data: dict[str, Any]) -> dict[str, Any]:
    """
    Normalize GoPlus response into the booleans the shield needs.

    GoPlus uses string "0"/"1" or sometimes objects. We normalize to bool/None
    so the shield code stays simple.
    """
    def b(key: str) -> Optional[bool]:
        v = data.get(key)
        if v is None or v == "":
            return None
        if isinstance(v, dict):
            v = v.get("status")
        try:
            return str(v) == "1"
        except Exception:
            return None

    flags = {
        "non_transferable": b("non_transferable"),
        "freezable": b("freezable"),
        "mintable": b("mintable"),
        "closable": b("closable"),
        "transfer_fee_upgradable": b("transfer_fee_upgradable"),
        "balance_mutable_authority": b("balance_mutable_authority"),
        "default_account_state_upgradable": b("default_account_state_upgradable"),
        "metadata_mutable": b("metadata_mutable"),
        "transfer_hook_upgradable": b("transfer_hook_upgradable"),
        "trusted_token": b("trusted_token"),
    }

    # Transfer fee may be present as a numeric percentage
    tf = data.get("transfer_fee")
    if isinstance(tf, dict):
        try:
            flags["transfer_fee_pct"] = float(tf.get("transfer_fee", 0))
        except Exception:
            flags["transfer_fee_pct"] = None
    else:
        try:
            flags["transfer_fee_pct"] = float(tf) if tf not in (None, "") else None
        except Exception:
            flags["transfer_fee_pct"] = None

    return flags
