# Meme Coin Trading Bot (Solana)

A modular Python 3.10+ trading bot for Solana meme coins with strict
on-chain security filters and automatic profit-taking.

> **⚠️ Risk warning.** Meme coins are extremely high-risk. Even with the
> filters this bot enforces, you *will* take losses on bad trades. Run in
> `paper` mode for at least a few days, ideally a week, before considering
> live mode. When you do switch to live, use a dedicated hot wallet funded
> with money you can afford to lose entirely.

## What it does

- **Watches** new liquidity pools on Raydium AMM v4 and Raydium CPMM via
  Solana WebSocket (`logsSubscribe`).
- **Filters** every new token through a security shield:
  - Honeypot / non-transferable / freezable check (GoPlus + on-chain)
  - Mint and freeze authorities renounced (on-chain via Solana RPC)
  - LP burnt or locked (on-chain heuristics; warning when Jupiter doesn't expose this signal)
  - Liquidity within configured range
  - No single non-LP holder > 5% of supply
  - No funding-cluster among top holders (Bubble-Maps-style one-hop check)
  - Transfer fee not abusive
- **Buys** with a fixed SOL size when all filters pass.
- **Exits** following a "Hit & Run" strategy:
  - At **2×** sell 50% of the initial position (capital recovery)
  - At **5×** sell another 10% of initial (tiered TP)
  - At **10×** sell another 10% of initial
  - After 2×, a **trailing stop** at 20% drawdown from peak closes the rest
  - Optional hard stop loss before 2× and a max-hold timeout
- **Notifies** every buy / sell / reject through Telegram.
- Runs in **paper** or **live** mode using the same code path.

## Project layout

```
meme_bot/
├── main.py                    # Entry point, signal handling
├── config.py                  # Settings loader (.env -> pydantic)
├── models.py                  # TokenInfo, Position, SecurityReport, ExitAction
├── core/
│   ├── bot.py                 # Orchestrator: wires everything together
│   ├── monitor.py             # WebSocket pool monitor (Raydium AMM v4, CPMM)
│   ├── price_feed.py          # Polls open positions and triggers exits
│   ├── trader.py              # PaperTrader + LiveTrader (Jupiter v6)
│   └── portfolio.py           # Position state + exit-decision logic
├── security/
│   ├── shield.py              # Aggregates checks into pass/reject decision
│   ├── goplus.py              # GoPlus Security API client
│   ├── jupiter.py             # Jupiter API client (overview/security/price)
│   └── helius_holders.py      # On-chain holder concentration + clusters
├── notifications/
│   └── telegram.py            # Telegram bot HTTP API
├── utils/
│   └── logger.py              # loguru-based logging with rotation
├── requirements.txt
├── .env.example
└── README.md
```

## Installation

```bash
# Python 3.10 or newer is required
git clone <your-repo> meme_bot && cd meme_bot
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and fill in the keys (see next section).

## API keys you need

| Service       | Why                                            | Required for           |
| ------------- | ---------------------------------------------- | ---------------------- |
| **Helius**    | Solana RPC + WebSocket (free tier ≥ 100k req)  | Always                 |
| **Jupiter**   | Liquidity, market data, price polling, swaps   | Recommended            |
| **GoPlus**    | Token security flags (honeypot, freezable…)    | Strongly recommended   |
| **Telegram**  | Trade notifications                            | Optional               |

Recommended setup:

1. **Helius** – sign up at <https://helius.dev>, copy your API key, and put it
   in `HELIUS_API_KEY`. Update `SOLANA_RPC_URL` and `SOLANA_WS_URL` to use
   your key (the example URLs already follow Helius' format).

2. **Jupiter** – an API key is *optional*. Without one the bot uses the free
   `lite-api.jup.ag` endpoints (lower rate limits). With one it uses
   `api.jup.ag` (Pro tier). Sign in at <https://portal.jup.ag/>, create a
   project, generate a key, and set `JUPITER_API_KEY`.

3. **GoPlus** – an API key is *optional* (the public endpoint works without
   one) but raises rate limits. Get one at <https://gopluslabs.io>.

4. **Telegram** – create a bot via [@BotFather](https://t.me/BotFather),
   copy the token, then DM [@userinfobot](https://t.me/userinfobot) to get
   your numeric `chat_id`. Set `TELEGRAM_ENABLED=true`,
   `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.

### Wallet (live mode only)

Create a **brand new** wallet for the bot. Never use your main wallet.

You can supply the secret key in either of two ways:

- `WALLET_PRIVATE_KEY_BASE58` – the raw base58 private key as exported by
  Phantom or Solflare. Easiest.
- `WALLET_KEYPAIR_PATH=/path/to/id.json` – a Solana CLI keypair file
  (`solana-keygen new -o id.json`). Useful if you keep your key in a file
  vault.

Fund it with a small amount of SOL — *enough for a handful of trades and
gas*, not your savings.

## Running

### Paper mode (default)

```bash
python main.py
```

You should see something like:

```
2026-05-09 12:00:00 | INFO     | core.bot:run:80 | [bot] starting in PAPER mode size=0.1 SOL slots=3
2026-05-09 12:00:00 | INFO     | core.monitor:_subscribe_all:74 | [monitor] subscribed to 675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8
```

In paper mode the bot:
- Receives real new-pool events
- Runs the *real* security shield against real on-chain data
- Simulates buys and sells with Jupiter prices and a 2% slippage haircut
- Persists state to `data/portfolio.json`
- Sends Telegram alerts (if enabled)

This is the right way to evaluate how often the shield rejects junk,
how the strategy performs, and whether your filters are too tight or too
loose, before risking any real SOL.

### Live mode

When you're ready:

```bash
# in .env
TRADING_MODE=live
WALLET_PRIVATE_KEY_BASE58=...
```

then run as before. The bot will:
- Print a warning at startup that you are in LIVE mode
- Execute swaps via Jupiter Aggregator v6
- Sign with the loaded keypair and submit to Solana via Helius RPC

Tip: start with a tiny `TRADE_SIZE_SOL` (e.g. `0.02`) and only one slot
(`MAX_OPEN_POSITIONS=1`) until you are sure execution and exits behave.

## Configuration reference

All knobs live in `.env`. The most important ones:

| Variable                  | Default | What it controls                                                  |
| ------------------------- | ------- | ----------------------------------------------------------------- |
| `TRADING_MODE`            | paper   | `paper` or `live`                                                 |
| `TRADE_SIZE_SOL`          | 0.1     | SOL spent per trade                                                |
| `MAX_OPEN_POSITIONS`      | 3       | Concurrent positions cap                                           |
| `SLIPPAGE_BPS`            | 1500    | Slippage tolerance for Jupiter (15% by default — meme reality)     |
| `PRIORITY_FEE_MICROLAMPORTS` | 200000 | Helps tx land in busy markets                                     |
| `MAX_TOP_HOLDER_PCT`      | 0.05    | Max share of a single non-LP wallet                                |
| `HOLDERS_TO_INSPECT`      | 20      | How many top holders to fetch funding info for (cluster check)     |
| `MIN_LIQUIDITY_USD`       | 15000   | Minimum pool liquidity to even consider                            |
| `MAX_LIQUIDITY_USD`       | 2000000 | Filter out blue chips                                              |
| `HARD_STOP_LOSS_RATIO`    | 0.5     | Sell everything below this multiplier before any TP. `0` disables. |
| `MAX_HOLD_MINUTES`        | 240     | Auto-exit after this long                                          |

## Security model

The shield treats every check with a clear severity:

- **Critical** (causes hard reject if missing): mint authority status, freeze
  authority status, liquidity range, holder concentration. The bot will not
  buy if any of these are unverifiable — better to miss the trade than to
  enter blind.
- **Important** (cause hard reject if violated, warning if API down): GoPlus
  honeypot/freezable/abusive-fee flags, LP locked or burnt, no funding
  cluster.
- **Soft** (warning only): mintable=true (mint authority active), mutable
  metadata, large `top10` Jupiter share. These show up in logs but don't
  block the trade by themselves (the on-chain mint/freeze authority check
  is what actually decides).

To tune: read `data/` and the rejection log to see which checks are firing
most often, and adjust thresholds in `.env` accordingly.

## How the cluster detection works

For each of the top-N non-LP holders we call `getSignaturesForAddress` and
walk the *oldest* signatures looking for the SOL transfer that funded the
wallet. The sender of that transfer is the wallet's "funder". If three or
more of the top holders share the same funder, the bot treats that as a
coordinated bundler / dev-team / rug setup and rejects the token.

This is a one-hop heuristic, deliberately cheap. It catches the common
case (one wallet seeding ten holders before launch) and misses sophisticated
multi-hop laundering. For deeper analysis you can plug in a graph service or
extend `helius_holders.py`.

## Logs and persistence

- All output is logged to console **and** to `logs/bot.log` (rotated at
  50 MB, kept 14 days, compressed).
- Open and recent closed positions are persisted to `data/portfolio.json`.
  On restart, the bot does **not** rehydrate open positions — this is
  intentional: stale state on a meme coin is worse than starting fresh.
  If you want resumption, edit `Portfolio._load`.

## Limitations and things to extend

- **Volume-spike entry**: the current entry trigger is "new pool +
  shield pass". You can tighten this by polling Jupiter's token `stats5m`
  buy/sell volume in the first 30 seconds before entering — wire that
  into `core/bot.py::_shield_worker` after the shield call.
- **EVM support**: the architecture is split deliberately so you can add
  an Ethereum/Base monitor + trader. The shield's interface (a
  `SecurityReport` object) is chain-agnostic.
- **Dynamic slippage / Jito bundles**: for sniper-style entries against
  competitors, you'll want Jito bundle submission and dynamic priority
  fees. Jupiter supports both.
- **More aggressive cluster detection**: extend
  `helius_holders.get_funding_source` to walk N hops, or batch via Helius'
  enhanced transactions API.
- **Per-trade circuit breaker**: easy to add — track recent PnL in
  `Portfolio` and short-circuit `open_position` when down too much.

## Quick smoke test

After installing dependencies and setting `.env`, you can verify the modules
load without actually starting the bot:

```bash
python -c "from core.bot import Bot; print('OK')"
```

If you see `OK`, you're good to run `python main.py`.
