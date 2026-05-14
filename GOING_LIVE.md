# Going Live

A short, opinionated guide for switching the bot from PAPER to LIVE with a
small throwaway wallet.

## 0. Reality check

Before you start: read [README's expectations on live performance][1].
**Paper trading systematically overestimates live results** because it
ignores slippage on fresh memes, MEV sandwiches, failed transactions, and
undetected rugs. A profitable paper run is necessary but not sufficient
evidence that the bot will be profitable live.

This guide assumes you have:

* ~0.5 SOL you can afford to lose entirely.
* A separate machine or container with reliable network connectivity.
* A Helius API key (free tier is fine for first tests).
* Read access to a terminal to monitor logs.

If any of that is missing, stop here and fix it first.

[1]: ../README.md

## 1. Create a fresh wallet

This must be a **throwaway** wallet. Do not use any wallet that has ever
held assets you care about.

```bash
python create_wallet.py
```

The script:

* Generates a fresh ed25519 keypair.
* Saves it as `./hot_wallet.json` (Solana CLI compatible format, mode 0600).
* Prints the public address (safe to share) and the base58 private key.
* Refuses to overwrite an existing `hot_wallet.json` (protects against
  accidental wallet loss).

Copy the **public address** somewhere safe — you'll send SOL to it. The
**private key** stays on this machine only.

## 2. Fund the wallet

From any existing wallet (Phantom, Backpack, an exchange withdrawal),
send a small amount of SOL — recommended **0.5 SOL** for first tests.

Verify the balance:

```bash
solana balance <public_address> --url https://mainnet.helius-rpc.com/?api-key=YOUR_KEY
```

You should see `0.5 SOL`. If you see `0`, the transfer hasn't landed yet
(wait ~30s) or you sent to the wrong address.

## 3. Configure `.env`

Edit `.env` (copy from `.env.example` if you haven't):

```bash
# --- Switch to live ---
TRADING_MODE=live

# --- Wallet ---
WALLET_KEYPAIR_PATH=./hot_wallet.json
# (or: WALLET_PRIVATE_KEY_BASE58=<the base58 key from step 1>)

# --- Trade sizing: START SMALL ---
TRADE_SIZE_SOL=0.01
MAX_OPEN_POSITIONS=2

# --- Risk brake ---
MAX_DRAWDOWN_PCT=0.5            # stop opening new positions at -50%
TX_FAILURE_THRESHOLD=3          # 3 failed tx in a row → pause
TX_FAILURE_COOLDOWN_S=300       # for 5 minutes

# --- Slippage ---
# 1500 bps = 15% on entry. Sells get 2x this automatically (capped at 50%).
# If you see many "no route" or quote failures, raise this.
SLIPPAGE_BPS=1500

# --- Priority fee ---
# Higher = your tx lands faster (better) but costs more (worse).
# 200_000 microlamports ≈ $0.001-0.005 per tx, decent default.
PRIORITY_FEE_MICROLAMPORTS=200000
```

Sanity check: with `TRADE_SIZE_SOL=0.01` and 0.5 SOL wallet you have ~50
trades of headroom before you run out. Keep that in mind when watching
the failure counter.

## 4. First run — supervised

Run the bot in a terminal you'll actually watch:

```bash
python main.py
```

The startup logs should show:

```
[trader] LIVE MODE - real funds at risk
[live] using Jupiter Pro endpoints (...)
[live] wallet loaded: 5axcsBsL42ayMnZ5...
[bot] starting in LIVE mode size=0.01 SOL slots=2
[bot] live wallet balance: 0.5000 SOL — risk manager calibrated against this baseline (max drawdown: 50%)
```

If you see `wallet loaded: ...` for an address that isn't yours, **stop
the bot immediately** and re-check your `.env`.

## 5. What to watch for

In the first 30 minutes, check that:

* **Buys execute** — look for `[live] tx confirmed: <sig>` after a
  `[live] BUY quote: ...`. Paste the signature into solscan.io to verify
  the swap.
* **Sells execute** — same thing, but for `[live] SELL quote: ...`. Sells
  matter more than buys: if buys land but sells don't, you accumulate
  bags faster than you can unload them.
* **Slippage is reasonable** — compare `exec_price` in the log to the
  Jupiter price you'd see on jup.ag. Anything beyond 10-15% deviation
  on a fresh meme is normal; beyond 30% means slippage_bps is too
  loose and you're getting MEV'd.
* **The risk manager logs sanely** — after each closed position you
  should see `[bot] risk status: realized=+X.XXXX SOL (+X.X%) failures=N`.

If you see:

* `[live] tx reverted on-chain: ...` repeatedly — the pool is hostile or
  your slippage is too tight. Stop the bot.
* `[risk] DRAWDOWN KILL SWITCH triggered` — the bot has stopped opening
  positions. Existing positions can still close. **Stop the bot and
  investigate before restarting.**
* Unexplained SOL leaving the wallet without corresponding log entries
  — the wallet may be compromised. Stop the bot, move remaining funds
  out, generate a new wallet.

## 6. Calibration — comparing paper to live

After at least 10 closed positions, compare:

* The realized PnL in SOL (`[bot] risk status: realized=...`).
* The same metrics from a paper run over a similar wallclock period.

Live PnL should be **noticeably worse than paper** — typically -30% to
-70% of paper's gain — due to slippage, MEV, and failed tx. If the gap is
larger than -70% something is structurally wrong (slippage too low,
priority fee too low, shield too permissive, or the chosen memes are
particularly hostile to small bots).

## 7. Scaling up

**Don't.** Not until at least 50 closed positions and consistent
positive live PnL across at least 24 hours of runtime.

When you do scale, do it in small steps:
* `TRADE_SIZE_SOL` from 0.01 → 0.02 → 0.05 → 0.1
* `MAX_OPEN_POSITIONS` from 2 → 3 → 5

Larger trade sizes hit larger slippage on small-liquidity memes. The
sweet spot is usually trade size ≈ 0.5–1% of pool liquidity.

## 8. Stopping safely

`Ctrl+C` in the bot's terminal. The bot will:

1. Cancel its background tasks.
2. Wait for any in-flight swap to either confirm or time out.
3. Close all HTTP/RPC clients cleanly.
4. Print final state.

**Open positions are NOT auto-closed on shutdown.** They remain in the
wallet and you need to sell them manually (via Phantom, jup.ag, or by
restarting the bot — it will resume monitoring).

If a position is stuck and the pool is dead, sell it manually through
jup.ag (use "swap any token") with high slippage tolerance, or accept
the loss and ignore it.
