"""
Generate a fresh, throwaway Solana wallet for live trading tests.

⚠️  THIS IS A TESTING TOOL. Read all the warnings.

Usage:
    python create_wallet.py

This will:
  1. Generate a fresh ed25519 keypair using solders
  2. Print the public address (safe to share — it's where you'll send SOL)
  3. Save the private key to ./hot_wallet.json (Solana CLI compatible format)
  4. Print the base58-encoded private key (so you can paste it into .env)

⚠️  SECURITY WARNINGS

  * Treat ./hot_wallet.json and the base58 key as PASSWORDS. Anyone who
    sees them owns the funds.
  * Do NOT commit hot_wallet.json. The repo's .gitignore already excludes
    *.json but double-check.
  * Use the smallest possible amount of SOL on this wallet (e.g. 0.5 SOL).
    Treat it as "burnt" the moment it touches the bot.
  * Never put your main wallet's private key in a bot config. Ever.
  * The bot stores the key in memory only; it doesn't transmit it. But
    if your machine is compromised, so is the wallet.

After running this:
  1. Fund the printed address with a small amount of SOL (e.g. 0.5).
     From your main wallet, send to the printed `Public address`.
  2. Add to your .env:
        WALLET_KEYPAIR_PATH=./hot_wallet.json
     OR (alternative form):
        WALLET_PRIVATE_KEY_BASE58=<the base58 key this script prints>
  3. Set TRADING_MODE=live
  4. Start with TRADE_SIZE_SOL=0.01 for the first runs.
"""
import json
import os
import sys
from pathlib import Path

try:
    from solders.keypair import Keypair
except ImportError:
    print("ERROR: solders is not installed. Run: pip install solders", file=sys.stderr)
    sys.exit(1)

try:
    import base58
except ImportError:
    print("ERROR: base58 is not installed. Run: pip install base58", file=sys.stderr)
    sys.exit(1)


OUT_PATH = Path("./hot_wallet.json")


def main() -> int:
    if OUT_PATH.exists():
        print(f"ERROR: {OUT_PATH} already exists. Refusing to overwrite.", file=sys.stderr)
        print(f"  If you really want a new wallet, delete or rename the existing file first.")
        print(f"  Existing file may contain real funds — back it up before deleting.")
        return 1

    kp = Keypair()
    secret_bytes = bytes(kp)  # 64 bytes: 32 secret + 32 pubkey
    pub = str(kp.pubkey())

    # Solana CLI keypair format: a JSON array of 64 ints
    OUT_PATH.write_text(json.dumps(list(secret_bytes)))
    # Make it readable only by current user (best-effort on Unix)
    try:
        os.chmod(OUT_PATH, 0o600)
    except Exception:
        pass

    b58 = base58.b58encode(secret_bytes).decode("ascii")

    print()
    print("=" * 60)
    print(" Fresh hot wallet generated")
    print("=" * 60)
    print()
    print(f" Public address (send SOL here to fund the wallet):")
    print()
    print(f"   {pub}")
    print()
    print(f" Saved keypair to: {OUT_PATH.resolve()}")
    print(f" Permissions: 0600 (owner read/write only on Unix)")
    print()
    print(f" Base58 private key (alternative for .env):")
    print()
    print(f"   {b58}")
    print()
    print("=" * 60)
    print(" Next steps")
    print("=" * 60)
    print()
    print(" 1. Fund the public address with a SMALL amount of SOL (e.g. 0.5).")
    print(" 2. Add to your .env:")
    print(f"      WALLET_KEYPAIR_PATH={OUT_PATH}")
    print(" 3. Set TRADING_MODE=live in .env")
    print(" 4. Set TRADE_SIZE_SOL=0.01 in .env for first runs")
    print(" 5. Start the bot: python main.py")
    print()
    print(" ⚠️  Anyone with access to the keypair file or the base58 key")
    print("    above can spend the SOL in this wallet. Treat as a password.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
