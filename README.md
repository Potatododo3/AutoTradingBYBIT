# Bitunix Futures Telegram Bot

A self-hosted Telegram bot for managing your [Bitunix](https://www.bitunix.com) Futures account. Place trades with a guided wizard, import positions placed in the app, manage TP/SL, monitor candle-close soft stop losses, and track performance — all from Telegram.

---

## Features

- **Guided trade wizard** — step-by-step: pair → direction → entry → risk → soft SL + timeframe → TP1/2/3 → DCA → confirm
- **Soft stop loss monitor** — no exchange order placed; bot watches candle closes and alerts you with Close/Ignore buttons
- **Position sync** — `/sync` imports any trade placed in the Bitunix app or web UI, attaches TPs automatically
- **Startup reconciliation** — on restart, detects limit entries that filled while offline and places TPs automatically
- **Native TP/SL** — uses Bitunix's TPSL system tied to `positionId`, compatible with hedge mode
- **Max leverage per symbol** — fetches the exchange's actual limit per coin before every trade, caps your configured max accordingly
- **Fixed risk balance** — set a specific dollar amount as your risk base (e.g. always 5% of $222.70), independent of live equity
- **Configurable TP splits** — default 40/30/30%, adjustable in `/settings`
- **Persistent settings** — all config saved to `settings.json`, survives restarts
- **Discord journaling** — logs trade opens, closes, and errors to a Discord webhook
- **Debug panel** — `/debug` tests every subsystem including live order probes, without placing real trades

---

## Setup

### 1. Requirements

```
Python 3.11+
pip install -r requirements.txt
```

### 2. Environment variables

Create a `.env` file:

```env
TELEGRAM_TOKEN=your_telegram_bot_token
AUTHORIZED_USER_ID=your_telegram_user_id

BITUNIX_API_KEY=your_api_key
BITUNIX_SECRET=your_api_secret

DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...   # optional
DB_PATH=trades.db
DEBUG_MODE=false
```

- **Telegram token**: message [@BotFather](https://t.me/BotFather)
- **Your user ID**: message [@userinfobot](https://t.me/userinfobot)
- **Bitunix API keys**: Bitunix → Account → API Management → enable Futures trading permission

### 3. Run

```bash
python main.py
```

---

## Commands

### Trading

| Command | Description |
|---------|-------------|
| `/trade` | Start the step-by-step trade wizard |
| `/cancel` | Abort an in-progress trade wizard |

**Wizard steps:**

| Step | Field | Notes |
|------|-------|-------|
| 1 | Pair | e.g. `BTCUSDT` |
| 2 | Direction | Long or Short |
| 3 | Entry | Limit price, or tap **Market** |
| 4 | Risk | `1%` of risk balance (or fixed `100$`) |
| 5 | Soft SL price | Your invalidation level — no exchange order placed |
| 6 | SL timeframe | Which candle must close beyond the level to trigger alert |
| 7 | TP1 | 40% of position closes here |
| 8 | TP2 | 30% closes here |
| 9 | TP3 | 30% closes here — TPSL also set here via native endpoint |
| — | DCA | Optional second entry order |
| — | Confirm | Review full summary before anything is placed |

**On confirm**, the bot:
1. Sets leverage (capped at symbol's exchange maximum)
2. Places entry order (market or limit)
3. Waits up to 5s for the position to appear and get `positionId`
4. Places TP1 + TP2 as limit close orders with `positionId` (required in hedge mode)
5. Places TP3 + native TPSL tied to the position
6. Registers soft SL monitoring in the background

If a limit entry hasn't filled yet, TP/SL placement is skipped with a notice to run `/sync` after fill.

---

### Soft Stop Loss

The soft SL system monitors candle closes in the background — **no exchange order is placed**. You are alerted and decide what to do.

| Command | Description |
|---------|-------------|
| `/setsl BTCUSDT 93000 1h` | Set or update soft SL level and timeframe |
| `/setsl BTCUSDT off` | Disable soft SL monitoring for a pair |

**Supported timeframes:** `15m` `30m` `1h` `4h` `1d`

**When a candle closes beyond your level**, you receive:

```
⚠️ SOFT SL BREACHED
BTCUSDT  🟢 LONG

  Candle close    $92,847.00
  Soft SL         $93,000.00
  Timeframe       1h
  Entry           $95,200.00

  The 1h candle closed below your soft SL.
  No position has been closed — your call.

[ 🔴 CLOSE POSITION ]  [ ⏩ IGNORE ]
```

- **Close** — flash-closes the position immediately, cancels all orders
- **Ignore** — dismisses the alert, sets a 2-candle cooldown before the next check

The monitor wakes up at each candle boundary (wall-clock aligned) + 5s buffer, not on a polling loop.

---

### Sync — trades placed outside the bot

| Command | Description |
|---------|-------------|
| `/sync` | Import all open exchange positions not already tracked by the bot |

Use `/sync` any time you open a trade in the Bitunix app, web UI, or after a limit entry fills.

The sync process:
1. Fetches all open positions from the exchange
2. Skips any already tracked
3. For each new position, reads pending TPSL orders → extracts SL + TP3
4. Reads pending limit orders → extracts TP1 + TP2
5. Saves everything to the database with the correct `positionId`

**On restart**, the bot automatically reconciles any pending limit entries:
- If the entry **filled** while offline → places TP1/TP2/TPSL immediately using actual filled qty
- If the entry was **cancelled** externally → removes it from tracking
- If the entry is **still pending** → leaves it alone

---

### Account

| Command | Description |
|---------|-------------|
| `/balance` | Total equity, available margin, in-use margin, utilisation bar |
| `/positions` | All open positions: size, entry, margin, unrealised PnL, ROE% |

---

### History & Stats

| Command | Description |
|---------|-------------|
| `/history` | Last 10 closed trades — entry → exit, PnL |
| `/stats` | Win rate, total PnL, average, best and worst trade |

---

### Management

| Command | Description |
|---------|-------------|
| `/setsl BTCUSDT 93000 1h` | Set soft SL level and monitoring timeframe |
| `/setsl BTCUSDT off` | Disable soft SL for a pair |
| `/modifysl BTCUSDT 93000` | Alias for `/setsl` (updates soft SL level) |
| `/cancelpair BTCUSDT` | Cancel all open orders for a pair (position stays open) |
| `/closeall` | Emergency market-close every tracked position |

---

### Settings (`/settings`)

Interactive inline menu with 5 sections:

| Section | Options |
|---------|---------|
| **Risk & Sizing** | Default risk %, risk balance (fixed $ base for risk calc), leverage |
| **TP Splits** | TP1/TP2/TP3 percentages with visual bar; warns if they don't sum to 100% |
| **Behaviour** | Trade confirmation toggle, close-all confirmation, auto SL→breakeven after TP1 |
| **Notifications** | TP hit alerts, SL hit alerts, PnL % threshold |
| **Display** | Compact mode, currency label |

#### Risk balance

By default, risk % is calculated from live equity. You can fix it to a specific dollar amount so your position sizes don't change as your balance fluctuates:

1. `/settings` → **Risk & Sizing**
2. Tap **✏️ Type value** under Risk balance
3. Send a message with the amount e.g. `222.70`
4. Send `0` to revert to live equity

The wizard prompt always shows which base is active: *"5.0% of $222.70"* or *"5.0% of live equity"*.

---

### Debug Panel (`/debug`)

Interactive test panel. All trade tests are dry-run (no real orders placed). Includes:

- Balance, positions, ticker API tests
- Position sizing calculation
- Full trade lifecycle dry-run
- Discord journal test
- API auth check
- **🔬 Probe: Place Order** — sends a real 0.001 BTC market order and immediately closes it; shows the exact request body and exchange response. Use this to diagnose parameter errors.
- **🔬 Probe: Set Leverage** — tests the leverage endpoint in isolation

---

## Architecture

```
main.py                Entry point — wires everything together
telegram_handlers.py   All Telegram commands + trade wizard
settings_handler.py    /settings menu + settings.json persistence
debug_handler.py       /debug test panel
soft_sl_monitor.py     Background candle-close monitor, one task per timeframe
messages.py            All message text and formatting
bitunix_client.py      Bitunix REST API client (signed requests, retry)
order_manager.py       Trade lifecycle: open, close, sync, modify, reconcile
risk_manager.py        Position sizing, leverage cap, risk base
database.py            SQLite persistence — trades.db
journal.py             Discord webhook logging + CSV journal
models.py              Dataclasses: TradeRecord, PositionSizing, etc.
config.py              .env loading
utils.py               Logging setup, helpers
```

---

## TP/SL Implementation

| Order | Method | Notes |
|-------|--------|-------|
| TP1 (40%) | Limit reduce-only + `positionId` | Required in hedge mode |
| TP2 (30%) | Limit reduce-only + `positionId` | Required in hedge mode |
| TP3 (30%) + SL | Native TPSL endpoint | Tied to position — survives partial fills |
| Soft SL | Bot monitors candle closes | No exchange order — you choose to close or ignore |

All close orders include `positionId` as required by Bitunix hedge-mode accounts.

---

## Risk Sizing

```
risk_base       = settings.risk_balance  (if set)  OR  live equity
risk_amount     = risk_base × risk_pct   (or fixed $ amount)
symbol_max_lev  = fetched from exchange position tiers per symbol
leverage        = min(settings.max_leverage, symbol_max_lev)
position_size   = risk_amount / stop_distance
margin_required = (position_size × entry_price) / leverage
```

The margin check always uses live available balance regardless of `risk_balance`.

---

## Database

SQLite with WAL mode. Auto-migrates schema on every startup — safe to run against an existing `trades.db`. Every trade stores: all order IDs, `positionId`, soft SL level + timeframe, entry/exit prices, PnL, status (`open` → `partial` → `closed`).

---

## Debug Mode

Set `DEBUG_MODE=true` in `.env`:
- All API reads (balance, positions, ticker) are real
- No orders are placed on the exchange
- Discord journal messages are still sent (labelled DEBUG)
- Every screen shows a warning banner

---

## Files Created at Runtime

| File | Purpose |
|------|---------|
| `trades.db` | SQLite — all trade records |
| `settings.json` | User config — persists across restarts |
| `journal.csv` | CSV log of all trade events |
| `bot.log` | Rotating log (5 MB × 3 backups) |