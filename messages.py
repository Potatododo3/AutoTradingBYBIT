"""
All bot message templates — visual design layer.

Aesthetic: trading terminal / bloomberg-dark. Monospaced data, clean
box-drawing borders, emoji as semantic colour signal only.
Every message reads like a real trading platform, not a chatbot.
"""
from datetime import datetime
from typing import Optional

from models import Side, TradeRecord, PositionSizing, TradeRequest, BitunixPosition


# ── Palette / tokens ──────────────────────────────────────────────────────────
# Telegram HTML only supports: <b> <i> <u> <s> <code> <pre> <a> — no colour.
# We use structure, whitespace, and emoji as the design vocabulary.

UP   = "▲"
DOWN = "▼"
BULL = "🟢"
BEAR = "🔴"
NEUT = "⬜"
WARN = "⚠️"
INFO = "ℹ️"
TICK = "✅"
CROSS= "✖"
FIRE = "🔥"
LOCK = "🔒"
GEAR = "⚙️"
CHRT = "📊"
BOOK = "📋"
HIST = "📜"
WALL = "💰"
RKET = "🚀"

DIV  = "─" * 28        # section divider
HDIV = "═" * 28        # heavy divider


def _pnl(value: float, prefix: str = "") -> str:
    sign  = "+" if value >= 0 else ""
    emoji = BULL if value >= 0 else BEAR
    return f"{emoji} {prefix}{sign}{value:,.2f}"


def _side_tag(side) -> str:
    if hasattr(side, "value"):
        s = side.value.upper()
    else:
        s = str(side).upper()
    # Handle both LONG/SHORT and BUY/SELL from exchange
    if s in ("LONG", "BUY"):
        return f"{BULL} LONG"
    return f"{BEAR} SHORT"


def _ts(dt: Optional[datetime]) -> str:
    if not dt:
        return "—"
    return dt.strftime("%d %b %Y  %H:%M UTC")


def _fmt(v) -> str:
    """Format a price preserving all significant digits, no trailing zeros, no sci notation."""
    if v is None:
        return "0"
    f = float(v)
    if f == 0:
        return "0"
    return f"{f:,.10f}".rstrip("0").rstrip(".")


# ── Start / Help / Commands ───────────────────────────────────────────────────

def start(debug_mode: bool = False) -> str:
    mode = f"\n\n{WARN}  <b>DEBUG MODE</b> — orders simulated, no real trades placed." if debug_mode else ""
    return (
        f"<b>BITUNIX FUTURES BOT</b>  {RKET}{mode}\n"
        f"<code>{HDIV}</code>\n\n"
        f"  Your personal trading terminal.\n"
        f"  Manage risk, place trades, monitor stops\n"
        f"  and track performance — all from Telegram.\n\n"
        f"<code>{DIV}</code>\n\n"
        f"  {CHRT}  /trade          Open a new position\n"
        f"  {BOOK}  /sync           Import trades from the app\n"
        f"  {WALL}  /balance        Wallet equity\n"
        f"  {BULL}  /positions      Open positions + PnL\n"
        f"  {GEAR}  /settings       Configure risk &amp; TP splits\n\n"
        f"<code>{DIV}</code>\n\n"
        f"  /commands  — full command list\n"
        f"  /help      — detailed guide"
    )


def help_msg() -> str:
    return (
        f"<b>COMMAND GUIDE</b>\n"
        f"<code>{HDIV}</code>\n\n"

        f"  {CHRT} <b>TRADING</b>\n"
        f"<code>{DIV}</code>\n"
        f"  /trade          Open a new position (guided wizard)\n"
        f"  /cancel         Abort wizard in progress\n\n"

        f"  <b>Wizard steps</b>\n"
        f"  <code>1</code>  Pair        e.g. <code>BTCUSDT</code>\n"
        f"  <code>2</code>  Direction   Long or Short\n"
        f"  <code>3</code>  Strategy    📐 Neil  /  🌊 Saltwayer  /  None\n"
        f"  <code>4</code>  Entry       Limit price or Market\n"
        f"  <code>5</code>  Risk        <code>1%</code> of balance  or  <code>100$</code> fixed\n"
        f"  <code>6</code>  Soft SL     Invalidation price level\n"
        f"  <code>7</code>  Timeframe   Candle that must close beyond SL\n"
        f"  <code>8</code>  TP1 / TP2 / TP3   Take profit levels\n"
        f"  <code>+</code>  DCA         Optional second entry (skip allowed)\n\n"
        f"  <i>Neil DCA = 2× entry size  |  Saltwayer DCA = 2.5× entry size</i>\n"
        f"  <i>Combined risk is split across entry + DCA when a strategy is set.</i>\n\n"

        f"  {WALL} <b>ACCOUNT</b>\n"
        f"<code>{DIV}</code>\n"
        f"  /balance        Total equity, available margin, utilisation\n"
        f"  /positions      Open positions with TP, DCA, SL and PnL\n\n"

        f"  {HIST} <b>HISTORY</b>\n"
        f"<code>{DIV}</code>\n"
        f"  /history        Last 10 closed trades\n"
        f"  /stats          Win rate, total PnL, best and worst trade\n\n"

        f"  {BOOK} <b>MANAGEMENT</b>\n"
        f"<code>{DIV}</code>\n"
        f"  /sync                Import positions opened outside the bot\n"
        f"  /resync <code>PAIR</code>        Force re-import a specific pair\n"
        f"  /setstrategy <code>PAIR neil|saltwayer|none</code>\n"
        f"                       Tag a trade with a strategy label\n"
        f"  /setsl <code>PAIR PRICE TF</code>   Set soft stop loss\n"
        f"         <code>PAIR off</code>         Disable monitoring\n"
        f"  /setleverage <code>PAIR N</code>    Set leverage for a symbol\n"
        f"  /cancelpair <code>PAIR</code>        Cancel all orders for a pair\n"
        f"  /closeall            Emergency market-close all tracked positions\n\n"

        f"  {GEAR} <b>SETTINGS  &amp;  TOOLS</b>\n"
        f"<code>{DIV}</code>\n"
        f"  /settings       Risk %, TP splits, balance config, notifications\n"
        f"  /debug          Test panel — API auth, sizing, flow (no real orders)"
    )


def commands_msg(debug_mode: bool = False) -> str:
    mode = f"  {WARN}  <b>DEBUG MODE ON</b> — no real orders placed\n\n" if debug_mode else ""
    return (
        f"<b>COMMANDS</b>\n"
        f"<code>{HDIV}</code>\n\n"
        f"{mode}"
        f"  <b>Trading</b>\n"
        f"  /trade           New position wizard\n"
        f"  /cancel          Abort wizard\n\n"
        f"  <b>Account</b>\n"
        f"  /balance         Wallet equity\n"
        f"  /positions       Open positions with orders\n"
        f"  /history         Last 10 closed trades\n"
        f"  /stats           Win rate &amp; PnL summary\n\n"
        f"  <b>Management</b>\n"
        f"  /sync            Import positions from exchange\n"
        f"  /resync          Force re-import a specific pair\n"
        f"  /setstrategy     Tag a trade with neil/saltwayer\n"
        f"  /setsl           Set soft stop loss\n"
        f"  /setleverage     Set leverage for a symbol\n"
        f"  /cancelpair      Cancel all orders for a pair\n"
        f"  /closeall        Emergency close all\n\n"
        f"  <b>Settings &amp; Tools</b>\n"
        f"  /settings        Configure risk, TP splits\n"
        f"  /debug           Test panel\n"
        f"  /help            Full command guide"
    )


# ── Wizard steps ──────────────────────────────────────────────────────────────


def _tp_pct(tp_num: int) -> int:
    """Return the configured TP split % for display (reads live settings)."""
    try:
        from settings_handler import get_settings
        s = get_settings()
        return int({1: s.tp1_pct, 2: s.tp2_pct, 3: s.tp3_pct}[tp_num] * 100)
    except Exception:
        return {1: 40, 2: 30, 3: 30}[tp_num]


def wizard_start(debug_mode: bool = False) -> str:
    note = f"\n  {WARN} <b>DEBUG MODE</b> — no real orders placed." if debug_mode else ""
    return (
        f"<b>NEW TRADE</b>  /  Step 1 of 8{note}\n"
        f"<code>{HDIV}</code>\n\n"
        f"  <b>TRADING PAIR</b>\n\n"
        f"  Enter the futures symbol:\n"
        f"  <code>BTCUSDT  ETHUSDT  SOLUSDT</code>"
    )


def wizard_step(step: int, label: str, confirmed: dict, prompt: str) -> str:
    """Generic wizard step with confirmation trail at top."""
    trail = ""
    for key, val in confirmed.items():
        trail += f"  <code>{key:<10}</code>  {val}\n"
    sep = f"<code>{DIV}</code>\n" if trail else ""
    return (
        f"<b>NEW TRADE</b>  /  Step {step} of 8\n"
        f"<code>{HDIV}</code>\n"
        f"{trail}"
        f"{sep}\n"
        f"  <b>{label.upper()}</b>\n\n"
        f"{prompt}"
    )


def _tp_summary_rows(req, sizing) -> str:
    """Render TP rows for wizard_summary with token qty per level."""
    from settings_handler import get_settings
    tps = [(1, req.tp1), (2, req.tp2), (3, req.tp3)]
    any_set = any(tp and tp > 0 for _, tp in tps)
    if not any_set:
        return f"  <code>{'TP1':<14}</code>  —\n\n"

    # Compute per-TP token quantities using the same split logic as order_manager
    s   = get_settings()
    ps  = sizing.position_size
    qp  = sizing.qty_precision
    tp1_qty = round(ps * s.tp1_pct, qp)
    tp2_qty = round(ps * s.tp2_pct, qp)
    tp3_qty = round(ps - tp1_qty - tp2_qty, qp)
    qtys = {1: tp1_qty, 2: tp2_qty, 3: tp3_qty}

    rows = ""
    for num, tp in tps:
        if tp and tp > 0:
            label = f"TP{num}  {_tp_pct(num)}%"
            qty   = qtys[num]
            rows += f"  <code>{label:<14}</code>  ${_fmt(tp)}  ({qty} tokens)\n"
    return rows + "\n"


def wizard_summary(req: TradeRequest, sizing: PositionSizing) -> str:
    entry_str    = "MARKET" if req.is_market else f"${_fmt(sizing.entry_price)}"
    side_str     = _side_tag(req.side)
    strategy_lbl = {"neil": "📐 Neil", "saltwayer": "🌊 Saltwayer"}.get(req.strategy or "", "")
    strategy_row = f"  <code>{'Strategy':<14}</code>  {strategy_lbl}\n" if strategy_lbl else ""
    dca_qty_lbl  = f"  ×{req.dca_qty}" if req.dca_qty else ""
    dca_row      = f"  <code>{'DCA':<14}</code>  ${_fmt(req.dca)}{dca_qty_lbl}\n" if req.dca else ""
    return (
        f"<b>TRADE SUMMARY</b>  /  {req.pair}\n"
        f"<code>{HDIV}</code>\n\n"
        f"  {side_str}\n"
        f"{strategy_row}\n"
        f"  <b>ENTRY & RISK</b>\n"
        f"<code>{DIV}</code>\n"
        f"  <code>{'Entry':<14}</code>  {entry_str}\n"
        f"  <code>{'Soft SL':<14}</code>  ${_fmt(sizing.sl_price)}\n"
        f"  <code>{'SL Distance':<14}</code>  ${_fmt(sizing.stop_distance)}\n"
        f"  <code>{'Risk':<14}</code>  ${sizing.risk_amount:,.2f}  ({sizing.risk_percent:.2f}%)\n"
        f"{dca_row}\n"
        f"  <b>TAKE PROFITS</b>\n"
        f"<code>{DIV}</code>\n"
        + _tp_summary_rows(req, sizing)
        + f"  <b>POSITION</b>\n"
        f"<code>{DIV}</code>\n"
        f"  <code>{'Leverage':<14}</code>  {sizing.leverage}×\n"
        f"  <code>{'Margin req.':<14}</code>  ${sizing.margin_required:,.2f}\n"
        f"  <code>{'Balance':<14}</code>  ${sizing.balance:,.2f}\n"
        f"  <code>{'Liq. Price':<14}</code>  ${_fmt(sizing.liquidation_price)}\n\n"
        f"<code>{DIV}</code>\n"
        f"  <i>Confirm to place orders on Bitunix.</i>"
    )


def _tp_opened_line(tp1: float, tp2: float, tp3: float) -> str:
    """TP row for trade_opened — lists only set TPs, or shows '—' if none."""
    active = [f"${_fmt(p)}" for p in (tp1, tp2, tp3) if p and p > 0]
    if active:
        return f"  <code>{'TP1 / 2 / 3':<12}</code>  {'  '.join(active)}\n\n"
    return f"  <code>{'TPs':<12}</code>  —\n\n"


def trade_opened(trade: TradeRecord, debug_mode: bool = False) -> str:
    note = f"\n  {WARN} <i>DEBUG — no real order placed.</i>" if debug_mode else ""
    side_str = _side_tag(trade.side)
    return (
        f"<b>TRADE OPENED</b>  {TICK}{note}\n"
        f"<code>{HDIV}</code>\n\n"
        f"  {side_str}  ·  <b>{trade.pair}</b>\n\n"
        f"  <code>{'ID':<12}</code>  <code>{trade.trade_id}</code>\n"
        f"  <code>{'Entry':<12}</code>  ${_fmt(trade.entry)}\n"
        f"  <code>{'Size':<12}</code>  {trade.position_size}\n"
        f"  <code>{'Leverage':<12}</code>  {trade.leverage}×\n"
        f"  <code>{'Soft SL':<12}</code>  ${_fmt(trade.sl)}\n"
        + _tp_opened_line(trade.tp1, trade.tp2, trade.tp3)
        + f"<code>{DIV}</code>\n"
        + f"  <i>Orders placed. Position is live.</i>"
    )


# ── Account ───────────────────────────────────────────────────────────────────

def balance(available: float, equity: float) -> str:
    in_use = equity - available
    util   = (in_use / equity * 100) if equity > 0 else 0
    bar_n  = int(util / 5)  # 20-char bar
    bar    = "█" * bar_n + "░" * (20 - bar_n)
    return (
        f"<b>FUTURES WALLET</b>\n"
        f"<code>{HDIV}</code>\n\n"
        f"  <code>{'Available':<16}</code>  <b>${available:>12,.2f}</b>\n"
        f"  <code>{'In Use':<16}</code>  ${in_use:>12,.2f}\n"
        f"<code>{DIV}</code>\n"
        f"  <code>{'Total Equity':<16}</code>  <b>${equity:>12,.2f}</b>\n\n"
        f"  <b>MARGIN UTILISATION</b>  {util:.1f}%\n"
        f"  <code>{bar}</code>"
    )


def positions(pos_list: list[BitunixPosition], active_trades: dict, classified: dict | None = None) -> str:
    if not pos_list:
        return (
            f"<b>OPEN POSITIONS</b>\n"
            f"<code>{HDIV}</code>\n\n"
            f"  No open positions.\n\n"
            f"  Use /trade to open one."
        )

    classified = classified or {}

    def _px(v) -> str:
        return f"${_fmt(v)}" if v and float(v) > 0 else "—"

    lines = [f"<b>OPEN POSITIONS</b>  ·  {len(pos_list)} active\n<code>{HDIV}</code>\n"]
    for p in pos_list:
        pnl_str  = _pnl(p.unrealized_pnl, "$")
        side_str = _side_tag(p.side)
        trade    = active_trades.get(p.symbol)
        orders   = classified.get(p.symbol, {})
        id_line  = f"  <code>{'Bot ID':<12}</code>  <code>{trade.trade_id}</code>\n" if trade else ""
        roe      = (p.unrealized_pnl / p.margin * 100) if p.margin else 0
        roe_str  = f"{'+ ' if roe >= 0 else ''}{roe:.1f}%".replace("+ ", "+")

        # ── TPs from live classified orders ───────────────────────────────────
        tp_rows = ""
        tps = orders.get("tps", [])
        if tps:
            for i, tp in enumerate(tps, 1):
                remaining = tp["remaining"]
                filled    = round(tp["qty"] - remaining, 8)
                partial   = f"  <i>({filled}/{tp['qty']} filled)</i>" if filled > 0 else ""
                tp_rows += f"  <code>{f'TP{i}':<12}</code>  ${_fmt(tp['price'])}  ×{remaining}{partial}\n"

        # ── DCAs from live classified orders + trade.dca fallback ──────────────
        dca_rows = ""
        dcas = orders.get("dcas", [])
        # Strategy — only show if explicitly set on the trade record
        detected_strategy = None
        if trade and trade.strategy:
            labels = {"neil": "📐 Neil", "saltwayer": "🌊 Saltwayer"}
            detected_strategy = labels.get(trade.strategy)
        strategy_row = f"  <code>{'Strategy':<12}</code>  {detected_strategy}\n" if detected_strategy else ""

        if dcas:
            for i, dca in enumerate(dcas, 1):
                remaining = dca["remaining"]
                filled    = round(dca["qty"] - remaining, 8)
                partial   = f"  <i>({filled}/{dca['qty']} filled)</i>" if filled > 0 else ""
                dca_rows += f"  <code>{f'DCA{i}':<12}</code>  ${_fmt(dca['price'])}  ×{remaining}{partial}\n"
        elif trade and trade.dca and float(trade.dca) > 0:
            # DCA was placed by the bot but is now filled (no longer in pending orders)
            dca_rows = f"  <code>{'DCA':<12}</code>  ${_fmt(trade.dca)}  <i>(filled)</i>\n"

        # ── Limit SL from live classified orders ──────────────────────────────
        sl_row = ""
        lsl = orders.get("sl")
        if lsl:
            sl_row = f"  <code>{'Limit SL':<12}</code>  ${_fmt(lsl['price'])}  ×{lsl['remaining']}\n"

        # ── Native TPSL stop ─────────────────────────────────────────────────
        # Source 1: trade.sl (bot DB record) — most accurate, includes BE tracking
        # Source 2: classified["native_sl"] — injected from exchange TPSL endpoint
        #           used when there is no bot trade record (synced / external position)
        tpsl_row = ""
        sl_price_raw = None
        if trade and trade.sl and float(trade.sl) > 0:
            sl_price_raw = float(trade.sl)
        elif not sl_price_raw:
            native = orders.get("native_sl", 0)
            if native and float(native) > 0:
                sl_price_raw = float(native)
        if sl_price_raw:
            is_be = abs(sl_price_raw - p.entry_price) < (p.entry_price * 0.0001)
            sl_label = "SL Breakeven" if is_be else "Stop Loss"
            tpsl_row = f"  <code>{sl_label:<12}</code>  ${_fmt(sl_price_raw)}\n"

        # ── Soft SL (candle monitor, no exchange order) ───────────────────────
        # Format: "4h SL  $0.0330"  (timeframe first, then price)
        soft_sl_row = ""
        if trade and trade.soft_sl_price and float(trade.soft_sl_price) > 0:
            tf_prefix = f"{trade.soft_sl_timeframe} " if trade.soft_sl_timeframe else ""
            soft_sl_label = f"{tf_prefix}SL"
            soft_sl_row = f"  <code>{soft_sl_label:<12}</code>  {_px(trade.soft_sl_price)}\n"

        lines.append(
            f"\n  <b>{p.symbol}</b>  ·  {side_str}  ·  {p.leverage}×\n"
            f"<code>{DIV}</code>\n"
            f"{id_line}"
            f"  <code>{'Size':<12}</code>  {p.size}\n"
            f"  <code>{'Entry':<12}</code>  ${_fmt(p.entry_price)}\n"
            f"  <code>{'Margin':<12}</code>  ${p.margin:,.2f}\n"
            f"  <code>{'Unreal. PnL':<12}</code>  {pnl_str}  <i>({roe_str} ROE)</i>\n"
            f"{strategy_row}"
            f"{tp_rows}"
            f"{dca_rows}"
            f"{sl_row}"
            f"{tpsl_row}"
            f"{soft_sl_row}"
        )
    return "\n".join(lines)


# ── History & stats ───────────────────────────────────────────────────────────

def history(trades: list) -> str:
    if not trades:
        return (
            f"<b>TRADE HISTORY</b>\n"
            f"<code>{HDIV}</code>\n\n"
            f"  No closed trades yet.\n"
            f"  History appears after your first trade closes."
        )
    lines = [f"<b>TRADE HISTORY</b>  ·  Last {len(trades)}\n<code>{HDIV}</code>"]
    for t in trades:
        pnl_str  = _pnl(t.realized_pnl, "$")
        side_str = _side_tag(t.side)
        lines.append(
            f"\n  <code>{t.trade_id}</code>  ·  <b>{t.pair}</b>  ·  {side_str}\n"
            f"  <code>{'Entry → Exit':<14}</code>  ${_fmt(t.entry)} → ${_fmt(t.exit_price)}\n"
            f"  <code>{'PnL':<14}</code>  {pnl_str}\n"
            f"  <code>{'Closed':<14}</code>  <i>{_ts(t.closed_at)}</i>"
        )
    return "\n".join(lines)


def stats(s: dict) -> str:
    if not s or s.get("total_trades", 0) == 0:
        return (
            f"<b>STATISTICS</b>\n"
            f"<code>{HDIV}</code>\n\n"
            f"  No closed trades yet.\n"
            f"  Stats appear after your first trade closes."
        )
    wr       = s['win_rate']
    bar_n    = int(wr / 5)
    wr_bar   = "█" * bar_n + "░" * (20 - bar_n)
    pnl_str  = _pnl(s['total_pnl'], "$")
    avg_str  = _pnl(s['avg_pnl'],   "$")
    return (
        f"<b>PERFORMANCE</b>\n"
        f"<code>{HDIV}</code>\n\n"
        f"  <b>RECORD</b>\n"
        f"<code>{DIV}</code>\n"
        f"  <code>{'Trades':<14}</code>  {s['total_trades']}\n"
        f"  <code>{'Wins':<14}</code>  {s['winning_trades']}\n"
        f"  <code>{'Losses':<14}</code>  {s['losing_trades']}\n\n"
        f"  <b>WIN RATE</b>  {wr:.1f}%\n"
        f"  <code>{wr_bar}</code>\n\n"
        f"  <b>P&L</b>\n"
        f"<code>{DIV}</code>\n"
        f"  <code>{'Total':<14}</code>  {pnl_str}\n"
        f"  <code>{'Average':<14}</code>  {avg_str}\n"
        f"  <code>{'Best':<14}</code>  {BULL} +${s['best_trade']:,.2f}\n"
        f"  <code>{'Worst':<14}</code>  {BEAR} -${abs(s['worst_trade']):,.2f}"
    )


# ── Errors ────────────────────────────────────────────────────────────────────

def api_error(context: str, err) -> str:
    response_preview = getattr(err, "response", "")[:300]
    return (
        f"<b>API ERROR</b>  {WARN}\n"
        f"<code>{HDIV}</code>\n\n"
        f"  <b>Context:</b>  {context}\n"
        f"  <b>Message:</b>  <code>{err}</code>\n\n"
        f"  <b>Response:</b>\n"
        f"  <pre>{response_preview}</pre>\n\n"
        f"  <i>Use /debug → API Auth for diagnostics.</i>"
    )


def validation_error(msg: str) -> str:
    return (
        f"<b>VALIDATION FAILED</b>  {WARN}\n"
        f"<code>{HDIV}</code>\n\n"
        f"  {msg}\n\n"
        f"  Use /trade to start a new trade."
    )