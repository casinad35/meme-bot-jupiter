"""
Quick diagnostic: check that JUPITER_API_KEY is loaded from .env and that
api.jup.ag actually responds.

Run: python check_jupiter.py
"""
from __future__ import annotations

import asyncio

from config import settings
from security.jupiter import JupiterClient


async def main() -> None:
    key = settings.jupiter_api_key
    if not key:
        print("❌ JUPITER_API_KEY is EMPTY.")
        print("   - Is your .env file in the same directory you run from?")
        print("   - Is the line `JUPITER_API_KEY=...` (no spaces around =)?")
        print("   - Did you restart your shell / venv after editing .env?")
        return

    masked = key[:4] + "..." + key[-4:] if len(key) > 8 else "***"
    print(f"✅ JUPITER_API_KEY loaded: {masked}  (length={len(key)})")

    client = JupiterClient(key)
    print(f"   base URL: {client.base}")
    print(f"   headers:  {client._headers()}")

    SOL = "So11111111111111111111111111111111111111112"
    USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

    print()
    print("→ Testing token_overview(SOL)...")
    ov = await client.token_overview(SOL)
    if ov:
        print(f"   symbol={ov['symbol']}  liquidity=${ov['liquidity']:,.0f}  "
              f"price=${ov['price']:.4f}")
    else:
        print("   ❌ token_overview returned None — check raw HTTP error above")

    print()
    print("→ Testing prices_multi([SOL, USDC])...")
    prices = await client.prices_multi([SOL, USDC])
    if prices:
        for mint, p in prices.items():
            print(f"   {mint[:8]}... = ${p:.4f}")
    else:
        print("   ❌ prices_multi returned empty")

    await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
