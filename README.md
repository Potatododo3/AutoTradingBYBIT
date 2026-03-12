# Bybit Trading Bot

A Telegram bot for managing perpetual futures trades on Bybit. Shares core logic with the Bitunix bot but connects to the Bybit API. Handles position sizing, TP/SL placement, DCA, soft stop losses, and trade journaling to Discord.

---

## Stack

- Python 3.13
- python-telegram-bot
- SQLite (persistent trade state)
- APScheduler (soft SL candle monitoring)
- Docker / Portainer on Ubuntu Server (home server)
- Discord webhooks for trade journal

> **Note:** Bybit blocks datacenter IPs (Railway, VPS, etc). Must run from a residential IP — home server only.

---

## Environment Variables

| Variable | Description |
|---|---|
| `TELEGRAM_TOKEN` | Bot token from BotFather |
| `TELEGRAM_CHAT_ID` | Your Telegram user ID |
| `BYBIT_API_KEY` | Bybit API key |
| `BYBIT_SECRET` | Bybit API secret |
| `BYBIT_BASE_URL` | `https://api.bybit.com` (mainnet) |
| `DISCORD_WEBHOOK_URL` | Discord webhook for trade journal |
| `RISK_BALANCE` | Fixed balance used for risk % calculations (0 = use live equity) |

---

## Bybit-Specific Notes

- Uses **hedge mode** — long and short positions tracked separately via `positionIdx`
- `retCode 34040` (SL/TP not modified) and `retCode 110043` (leverage not modified) treated as no-ops — no error raised
- `closedPnl` from Bybit order history is already net of fees
- TPSL orders use Bybit's native stop endpoint, bound to `positionIdx`

---

## Core Features

### Trade Wizard (`/trade`)
Multi-step wizard for opening a new trade. Collects:
1. Pair
2. Side (Long / Short)
3. Entry type (Market or Limit) and price
4. Stop Loss price and type (Hard SL on exchange, or Soft SL with candle timeframe)
5. TP1 / TP2 / TP3 prices (optional)
6. DCA price (optional)
7. Strategy (Neil 2x / Saltwayer 2.5x / None)
8. Risk amount (% of balance or fixed $)
9. Leverage

On confirm: places entry order, TP limit orders, hard SL via TPSL, and DCA limit order. Soft SL registers a candle monitor instead of placing an exchange order.

### Position Sizing
Risk-based sizing: `qty = risk_amount / stop_distance`. TP quantities split by configurable percentages (TP1 % / TP2 % / remainder to TP3).

### Strategies
- **Neil**: DCA qty = 2x entry size
- **Saltwayer**: DCA qty = 2.5x entry size, total 5% risk split across entry and DCA

### Soft SL Monitor
Watches candle closes for trades with a soft SL set. Fires a Telegram alert with Close / Ignore buttons when the candle closes beyond the SL level. Buffer of 15s after candle boundary before fetching to allow exchange to finalize the candle.

### DCA Fill Detection
Poller checks pending DCA orders every cycle. On fill: cancels old TP orders, fetches live position size, recalculates TP splits, and re-places all TP orders at correct proportions.

### TP Fill Detection
Poller detects TP1 / TP2 / TP3 fills and logs them to Discord journal.

### Discord Journal
Logs trade open, close, SL moves, and TP hits to a Discord channel via webhook. PnL for bot-initiated closes uses ticker price estimate; SL/TP/external closes use `realizedPNL` from Bybit order history.

---

## Commands

### Info
| Command | Description |
|---|---|
| `/start` | Greeting and bot status |
| `/help` | Overview of bot features |
| `/commands` | Full command reference |
| `/balance` | Futures wallet balance and utilization |
| `/positions` | All active trades with entry, TP, SL, PnL, and DCA status |
| `/history` | Last 10 closed trades |
| `/stats` | Win rate, avg RR, total PnL summary |

### Trading
| Command | Description |
|---|---|
| `/trade` | Open trade wizard |
| `/closeall` | Close all open positions at market |
| `/cancelpair PAIR` | Cancel all pending orders for a pair |
| `/sync` | Import existing exchange position into bot (no new orders placed) |
| `/resync PAIR` | Re-import a specific pair, refresh order IDs from exchange |

### Stop Loss
| Command | Description |
|---|---|
| `/modifysl PAIR PRICE` | Update SL in DB and move exchange TPSL order |
| `/movesl PAIR PRICE` | Move exchange SL order only (no DB update) |
| `/movesl PAIR be` | Move exchange SL to breakeven |
| `/setsl PAIR PRICE TIMEFRAME` | Set or update soft SL (candle monitor, no exchange order) |
| `/setsl PAIR off` | Disable soft SL monitoring for a pair |

Valid timeframes for soft SL: `15m` `30m` `1h` `4h` `Daily`

### Take Profit
| Command | Description |
|---|---|
| `/tp PAIR PRICE [QTY]` | Add a TP order. QTY optional — defaults to TP slot sizing |
| `/fixtp PAIR` | Cancel all TP orders and re-place proportionally from live position size |

### DCA
| Command | Description |
|---|---|
| `/dca PAIR PRICE RISK NEW_SL` | Add a DCA limit order. Shows three sizing options: strategy multiplier, exact combined risk formula, and risk-sized |

The exact combined risk option back-calculates entry and DCA quantities so that `entry_risk + DCA_risk = RISK` exactly at `NEW_SL`, regardless of how the original entry was sized.

### Settings
| Command | Description |
|---|---|
| `/setstrategy PAIR neil\|saltwayer\|none` | Assign a strategy to a tracked trade |
| `/setleverage PAIR LEVERAGE` | Set leverage on exchange and update bot config |

---

## DCA Sizing Options (on `/dca` confirmation)

| Button | Logic |
|---|---|
| Strategy (2x / 2.5x) | Locks qty to `entry_size x multiplier`. Risk shown is what that qty actually risks at the new SL. |
| Exact total risk | Back-calculates corrected entry qty so `entry_risk + DCA_risk = your input` exactly at new SL. DCA qty = `corrected_entry x multiplier`. |
| Risk-sized | Standard `risk / stop_distance`. Ignores strategy. |

---

## Soft SL vs Hard SL

| | Hard SL | Soft SL |
|---|---|---|
| Exchange order | Yes (TPSL) | No |
| Triggers on | Price touch | Candle close beyond level |
| Set via | Trade wizard | Trade wizard or `/setsl` |
| Alert | None (silent fill) | Telegram alert with Close / Ignore |
| Timeframes | N/A | 15m, 30m, 1h, 4h, Daily |

---

## Deployment

Runs as a Docker container via Portainer stack on a Lenovo laptop (AMD A6, 4GB RAM, Ubuntu Server). Must run from a residential IP — Bybit blocks datacenter/VPS IPs.

```yaml
services:
  bybit-bot:
    image: python:3.13-slim
    working_dir: /app
    volumes:
      - ./:/app
    env_file: .env
    command: python main.py
    restart: unless-stopped
```

---

## Database

SQLite file at `/app/trades.db`. Schema auto-migrates on startup. Stores all active and closed trades including entry, TP/SL prices, order IDs, position index (hedge mode), soft SL state, strategy, seen close order IDs (for dedup), and exit price.