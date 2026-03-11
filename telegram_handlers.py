import asyncio
import logging
from typing import Optional
from warnings import filterwarnings

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters
)
from telegram.warnings import PTBUserWarning

import messages as M
from messages import _fmt  # price formatter — no trailing zeros, no sci notation
from bybit_client import BybitClient
from config import AUTHORIZED_USER_ID, DEBUG_MODE
from database import Database
from journal import Journal
from models import (
    TradeRequest, Side, RiskType, PositionSizing,
    ValidationError, APIError, InsufficientMarginError, DuplicateTradeError
)
from order_manager import OrderManager
from risk_manager import RiskManager
from settings_handler import get_settings
from soft_sl_monitor import SoftSLMonitor, PERIOD_SECONDS
from utils import parse_risk

filterwarnings(action="ignore", message=r".*CallbackQueryHandler", category=PTBUserWarning)

logger = logging.getLogger(__name__)

# ── Conversation states ───────────────────────────────────────────────────────
(
    ASK_PAIR, ASK_SIDE, ASK_STRATEGY, ASK_ENTRY, ASK_RISK,
    ASK_SL, ASK_SL_TIMEFRAME, ASK_TP1, ASK_TP2, ASK_TP3, ASK_DCA, CONFIRM_TRADE,
) = range(12)

TRADE_KEY = "trade_wizard"


# ── Auth decorator ────────────────────────────────────────────────────────────

def auth_required(func):
    async def wrapper(*args, **kwargs):
        update = next((a for a in args if isinstance(a, Update)), None)
        uid = update.effective_user.id if (update and update.effective_user) else None
        if uid != AUTHORIZED_USER_ID:
            logger.warning(f"Unauthorized access from uid={uid}")
            if update and update.message:
                await update.message.reply_text("⛔ Access denied.")
            elif update and update.callback_query:
                await update.callback_query.answer("⛔ Access denied.", show_alert=True)
            return ConversationHandler.END
        return await func(*args, **kwargs)
    return wrapper


# ── Shared UI helpers ─────────────────────────────────────────────────────────

def _abort_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✖  Abort trade", callback_data="wizard:cancel")
    ]])


def _trail(d: dict) -> str:
    """Render confirmed values at top of each wizard step."""
    if not d:
        return ""
    lines = ""
    labels = {
        "pair": "Pair", "side": "Side", "entry": "Entry",
        "risk": "Risk", "sl": "Stop Loss",
        "tp1": "TP1", "tp2": "TP2", "tp3": "TP3",
    }
    for key, val in d.items():
        if key.startswith("_"):
            continue
        label = labels.get(key, key)
        lines += f"  <code>{label:<10}</code>  {val}\n"
    return lines + f"<code>{'─'*28}</code>\n\n" if lines else ""


class BotHandlers:
    def __init__(
        self,
        client: BybitClient,
        order_manager: OrderManager,
        risk_manager: RiskManager,
        journal: Journal,
        db: Database,
    ) -> None:
        self._client  = client
        self._om      = order_manager
        self._rm      = risk_manager
        self._jnl     = journal
        self._db      = db
        self._app     = None   # set in register()
        self._chat_id = AUTHORIZED_USER_ID  # pre-seeded — same as chat_id in private DMs
        # Soft SL monitor — fires breach alerts via _on_soft_sl_breach
        self._soft_sl_monitor = SoftSLMonitor(
            client=client,
            get_active_trades=order_manager.get_all_active,
            on_breach=self._on_soft_sl_breach,
        )
        self._entry_poller_task: Optional[asyncio.Task] = None
        self._tp_poller_task:    Optional[asyncio.Task] = None
        self._pos_poller_task:   Optional[asyncio.Task] = None

    def register(self, app: Application) -> None:
        trade_conv = ConversationHandler(
            entry_points=[CommandHandler("trade", self.trade_start)],
            states={
                ASK_PAIR:  [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.trade_pair),
                    CallbackQueryHandler(self._wiz_cancel, pattern="^wizard:cancel$"),
                ],
                ASK_SIDE:  [
                    CallbackQueryHandler(self.trade_side,   pattern="^side:"),
                    CallbackQueryHandler(self._wiz_cancel,  pattern="^wizard:cancel$"),
                ],
                ASK_STRATEGY: [
                    CallbackQueryHandler(self.trade_strategy, pattern="^strategy:"),
                    CallbackQueryHandler(self._wiz_cancel,    pattern="^wizard:cancel$"),
                ],
                ASK_ENTRY: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.trade_entry),
                    CallbackQueryHandler(self.trade_entry_market, pattern="^entry:market$"),
                    CallbackQueryHandler(self._wiz_cancel,  pattern="^wizard:cancel$"),
                ],
                ASK_RISK:  [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.trade_risk),
                    CallbackQueryHandler(self._wiz_cancel,  pattern="^wizard:cancel$"),
                ],
                ASK_SL:    [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.trade_sl),
                    CallbackQueryHandler(self._wiz_cancel,  pattern="^wizard:cancel$"),
                ],
                ASK_SL_TIMEFRAME: [
                    CallbackQueryHandler(self.trade_sl_timeframe, pattern="^sltf:"),
                    CallbackQueryHandler(self._wiz_cancel,  pattern="^wizard:cancel$"),
                ],
                ASK_TP1:   [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.trade_tp1),
                    CallbackQueryHandler(self.trade_tp_skip_all, pattern="^tp:skip_all$"),
                    CallbackQueryHandler(self._wiz_cancel,  pattern="^wizard:cancel$"),
                ],
                ASK_TP2:   [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.trade_tp2),
                    CallbackQueryHandler(self.trade_tp2_skip, pattern="^tp:skip_tp2$"),
                    CallbackQueryHandler(self._wiz_cancel,  pattern="^wizard:cancel$"),
                ],
                ASK_TP3:   [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.trade_tp3),
                    CallbackQueryHandler(self.trade_tp3_skip, pattern="^tp:skip_tp3$"),
                    CallbackQueryHandler(self._wiz_cancel,  pattern="^wizard:cancel$"),
                ],
                ASK_DCA:   [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.trade_dca),
                    CallbackQueryHandler(self.trade_dca_skip, pattern="^dca:skip$"),
                    CallbackQueryHandler(self._wiz_cancel,  pattern="^wizard:cancel$"),
                ],
                CONFIRM_TRADE: [
                    CallbackQueryHandler(self.trade_confirm,        pattern="^trade:confirm$"),
                    CallbackQueryHandler(self.trade_cancel_confirm, pattern="^trade:cancel$"),
                ],
            },
            fallbacks=[
                CommandHandler("cancel", self._cmd_cancel_wizard),
                CommandHandler("trade",  self.trade_start),
            ],
            per_user=True, per_chat=True, per_message=False, allow_reentry=True,
        )

        app.add_handler(trade_conv)
        app.add_handler(CommandHandler("start",      self.cmd_start))
        app.add_handler(CommandHandler("help",       self.cmd_help))
        app.add_handler(CommandHandler("commands",   self.cmd_commands))
        app.add_handler(CommandHandler("balance",    self.cmd_balance))
        app.add_handler(CommandHandler("positions",  self.cmd_positions))
        app.add_handler(CommandHandler("closeall",   self.cmd_closeall))
        app.add_handler(CommandHandler("cancelpair", self.cmd_cancelpair))
        app.add_handler(CommandHandler("history",    self.cmd_history))
        app.add_handler(CommandHandler("stats",      self.cmd_stats))
        app.add_handler(CommandHandler("sync",       self.cmd_sync))
        app.add_handler(CommandHandler("resync",     self.cmd_resync))
        app.add_handler(CommandHandler("modifysl",   self.cmd_modifysl))
        app.add_handler(CommandHandler("movesl",     self.cmd_movesl))
        app.add_handler(CommandHandler("setsl",      self.cmd_setsl))
        app.add_handler(CommandHandler("setstrategy", self.cmd_setstrategy))
        app.add_handler(CommandHandler("setleverage", self.cmd_setleverage))
        app.add_handler(CommandHandler("tp",          self.cmd_tp))
        app.add_handler(CommandHandler("dca",         self.cmd_dca))
        app.add_handler(CallbackQueryHandler(self._handle_addtp,  pattern="^addtp:"))
        app.add_handler(CallbackQueryHandler(self._handle_adddca, pattern="^adddca:"))
        app.add_handler(CallbackQueryHandler(self.handle_callback,  pattern="^closeall:"))
        app.add_handler(CallbackQueryHandler(self._soft_sl_action,  pattern="^softsl:"))

    def start_monitor(self) -> None:
        """Start background monitors. Called from main.py after startup."""
        self._soft_sl_monitor.start()
        self._entry_poller_task = asyncio.create_task(
            self._om.run_entry_fill_poller(self._notify),
            name="entry_fill_poller",
        )
        self._tp_poller_task = asyncio.create_task(
            self._om.run_tp_fill_poller(self._notify),
            name="tp_fill_poller",
        )
        self._pos_poller_task = asyncio.create_task(
            self._om.run_position_close_poller(self._notify),
            name="position_close_poller",
        )
        logger.info("Entry, TP, and position close pollers started.")

    async def stop_monitor(self) -> None:
        """Stop all background monitors."""
        await self._soft_sl_monitor.stop()
        if self._entry_poller_task and not self._entry_poller_task.done():
            self._entry_poller_task.cancel()
            try:
                await self._entry_poller_task
            except asyncio.CancelledError:
                pass
        if self._tp_poller_task and not self._tp_poller_task.done():
            self._tp_poller_task.cancel()
            try:
                await self._tp_poller_task
            except asyncio.CancelledError:
                pass
        if self._pos_poller_task and not self._pos_poller_task.done():
            self._pos_poller_task.cancel()
            try:
                await self._pos_poller_task
            except asyncio.CancelledError:
                pass

    def register_unknown_handler(self, app: Application) -> None:
        app.add_handler(MessageHandler(filters.COMMAND, self.cmd_unknown))

    # ── Wizard ────────────────────────────────────────────────────────────────

    @auth_required
    async def trade_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        self._capture_chat(update)
        context.user_data[TRADE_KEY] = {}
        s = get_settings()
        await update.message.reply_text(
            M.wizard_start(DEBUG_MODE),
            parse_mode="HTML",
            reply_markup=_abort_kb(),
        )
        return ASK_PAIR

    @auth_required
    async def trade_pair(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        pair = update.message.text.strip().upper()
        if not pair.isalnum():
            await update.message.reply_text(
                f"<b>INVALID SYMBOL</b>\n\n"
                f"  Pair names are alphanumeric only.\n"
                f"  Try: <code>BTCUSDT</code>  <code>ETHUSDT</code>  <code>SOLUSDT</code>",
                parse_mode="HTML", reply_markup=_abort_kb(),
            )
            return ASK_PAIR

        if self._om.has_active_trade(pair):
            await update.message.reply_text(
                f"<b>DUPLICATE TRADE</b>  ⚠️\n"
                f"<code>{'─'*28}</code>\n\n"
                f"  Already tracking a position on <b>{pair}</b>.\n"
                f"  Use /positions to view it.",
                parse_mode="HTML",
            )
            return ConversationHandler.END

        d = context.user_data[TRADE_KEY]
        d["pair"] = pair

        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🟢  LONG  (Buy)",   callback_data="side:long"),
                InlineKeyboardButton("🔴  SHORT  (Sell)", callback_data="side:short"),
            ],
            [InlineKeyboardButton("✖  Abort trade", callback_data="wizard:cancel")],
        ])
        await update.message.reply_text(
            f"<b>NEW TRADE</b>  /  Step 2 of 9\n"
            f"<code>{'═'*28}</code>\n\n"
            f"{_trail(d)}"
            f"  <b>DIRECTION</b>\n\n"
            f"  Are you going long or short?",
            parse_mode="HTML", reply_markup=kb,
        )
        return ASK_SIDE

    @auth_required
    async def trade_side(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        side = query.data.split(":")[1]
        d = context.user_data[TRADE_KEY]
        d["side"] = side
        d["_side_label"] = "🟢 LONG" if side == "long" else "🔴 SHORT"

        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📐  Neil",       callback_data="strategy:neil"),
                InlineKeyboardButton("🌊  Saltwayer",  callback_data="strategy:saltwayer"),
            ],
            [InlineKeyboardButton("⬜  No strategy", callback_data="strategy:none")],
            [InlineKeyboardButton("✖  Abort trade",  callback_data="wizard:cancel")],
        ])
        await query.edit_message_text(
            f"<b>NEW TRADE</b>  /  Step 3 of 9\n"
            f"<code>{'═'*28}</code>\n\n"
            f"{_trail({'pair': d['pair'], 'side': d['_side_label']})}"
            f"  <b>STRATEGY</b>\n\n"
            f"  <b>Neil</b>  —  DCA is 2× entry token size\n"
            f"  <b>Saltwayer</b>  —  DCA is 2.5× entry token size\n"
            f"  <b>None</b>  —  enter DCA size manually",
            parse_mode="HTML", reply_markup=kb,
        )
        return ASK_STRATEGY

    @auth_required
    async def trade_strategy(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        strategy = query.data.split(":")[1]  # "neil" | "saltwayer" | "none"
        d = context.user_data[TRADE_KEY]
        d["strategy"] = None if strategy == "none" else strategy

        strategy_labels = {"neil": "📐 Neil", "saltwayer": "🌊 Saltwayer", "none": "—"}
        strategy_label  = strategy_labels.get(strategy, "—")
        d["_strategy_label"] = strategy_label
        # Neil/Saltwayer now act as a tag only.
        # DCA multiplier (2×/2.5×) is applied in _build_confirm IF a DCA price is entered.

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⚡  Market order  (fill immediately)", callback_data="entry:market")],
            [InlineKeyboardButton("✖  Abort trade", callback_data="wizard:cancel")],
        ])
        strat_note = ""
        if strategy in ("neil", "saltwayer"):
            mult = "2×" if strategy == "neil" else "2.5×"
            strat_note = (
                f"\n  <i>{strategy_labels[strategy]} tag applied — if you add a DCA price,\n"
                f"  the DCA order will be sized at {mult} your entry qty.</i>"
            )
        await query.edit_message_text(
            f"<b>NEW TRADE</b>  /  Step 4 of 9\n"
            f"<code>{'═'*28}</code>\n\n"
            f"{_trail({'pair': d['pair'], 'side': d['_side_label'], 'strategy': strategy_label})}"
            f"  <b>ENTRY PRICE</b>\n\n"
            f"  Type a price for a <b>limit order</b>,\n"
            f"  or tap <b>Market order</b> to fill now:{strat_note}",
            parse_mode="HTML", reply_markup=kb,
        )
        return ASK_ENTRY

    @auth_required
    async def trade_entry_market(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        d = context.user_data[TRADE_KEY]
        d["entry"] = "market"
        s = get_settings()
        strategy_label = d.get('_strategy_label', '—')
        await query.edit_message_text(
            f"<b>NEW TRADE</b>  /  Step 5 of 9\n"
            f"<code>{'═'*28}</code>\n\n"
            f"{_trail({'pair': d['pair'], 'side': d['_side_label'], 'strategy': strategy_label, 'entry': 'MARKET'})}"
            f"  <b>RISK AMOUNT</b>\n\n"
            f"  How much to risk on this trade?\n\n"
            f"  <code>1%</code>   — 1% of your balance\n"
            f"  <code>50$</code>  — fixed $50\n\n"
            f"  <i>Default: {s.default_risk_pct:.1f}% of "
            f"{'$'+f'{s.risk_balance:,.2f}' if s.risk_balance > 0 else 'live equity'}</i>",
            parse_mode="HTML", reply_markup=_abort_kb(),
        )
        return ASK_RISK

    @auth_required
    async def trade_entry(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        text = update.message.text.strip()
        try:
            price = float(text)
            if price <= 0: raise ValueError
        except ValueError:
            await update.message.reply_text(
                f"<b>INVALID PRICE</b>\n\n"
                f"  Enter a number e.g. <code>95000</code>.\n"
                f"  Or tap Market order below:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⚡  Market order", callback_data="entry:market")],
                    [InlineKeyboardButton("✖  Abort trade",  callback_data="wizard:cancel")],
                ]),
            )
            return ASK_ENTRY

        d = context.user_data[TRADE_KEY]
        d["entry"] = price
        s = get_settings()
        strategy_label = d.get('_strategy_label', '—')
        await update.message.reply_text(
            f"<b>NEW TRADE</b>  /  Step 5 of 9\n"
            f"<code>{'═'*28}</code>\n\n"
            f"{_trail({'pair': d['pair'], 'side': d['_side_label'], 'strategy': strategy_label, 'entry': f'${_fmt(price)}'})}"
            f"  <b>RISK AMOUNT</b>\n\n"
            f"  How much to risk on this trade?\n\n"
            f"  <code>1%</code>   — 1% of your balance\n"
            f"  <code>50$</code>  — fixed $50\n\n"
            f"  <i>Default: {s.default_risk_pct:.1f}% of "
            f"{'$'+f'{s.risk_balance:,.2f}' if s.risk_balance > 0 else 'live equity'}</i>",
            parse_mode="HTML", reply_markup=_abort_kb(),
        )
        return ASK_RISK

    @auth_required
    async def trade_risk(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        text = update.message.text.strip()
        try:
            risk_value, risk_type_str = parse_risk(text)
        except ValueError as e:
            await update.message.reply_text(
                f"<b>INVALID RISK</b>\n\n  {e}\n\n"
                f"  Use <code>1%</code> or <code>100$</code>:",
                parse_mode="HTML", reply_markup=_abort_kb(),
            )
            return ASK_RISK

        s = get_settings()
        d = context.user_data[TRADE_KEY]
        d["risk_value"] = risk_value
        d["risk_type"]  = risk_type_str
        side = d["side"]
        sl_hint = "below entry" if side == "long" else "above entry"

        await update.message.reply_text(
            f"<b>NEW TRADE</b>  /  Step 6 of 9\n"
            f"<code>{'═'*28}</code>\n\n"
            f"{_trail({'pair': d['pair'], 'side': d['_side_label'], 'risk': text})}"
            f"  <b>SOFT STOP LOSS PRICE</b>\n\n"
            f"  Enter your stop loss price level.\n"
            f"  No order is placed — the bot monitors candle\n"
            f"  closes and alerts you when this level is breached.\n\n"
            f"  <i>For a {side.upper()}, set this {sl_hint}.</i>",
            parse_mode="HTML", reply_markup=_abort_kb(),
        )
        return ASK_SL

    @auth_required
    async def trade_sl(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        try:
            sl = float(update.message.text.strip())
            if sl <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "<b>INVALID PRICE</b>  Enter a number:",
                parse_mode="HTML", reply_markup=_abort_kb(),
            )
            return ASK_SL

        d = context.user_data[TRADE_KEY]
        d["sl"] = sl

        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("15m",   callback_data="sltf:15m"),
                InlineKeyboardButton("30m",   callback_data="sltf:30m"),
                InlineKeyboardButton("1h",    callback_data="sltf:1h"),
            ],
            [
                InlineKeyboardButton("4h",    callback_data="sltf:4h"),
                InlineKeyboardButton("Daily", callback_data="sltf:Daily"),
            ],
            [InlineKeyboardButton("✖  Abort trade", callback_data="wizard:cancel")],
        ])
        sl_str = f"${_fmt(sl)}"
        hdr = f"<b>NEW TRADE</b>  /  Step 7 of 9\n<code>{"=" * 28}</code>\n\n"
        trail = _trail({'pair': d['pair'], 'side': d['_side_label'], 'sl': sl_str})
        body = (
            f"  <b>SL TIMEFRAME</b>\n\n"
            f"  Which candle must close beyond <code>{sl_str}</code>?\n"
            f"  Choose the timeframe for candle-close monitoring:"
        )
        await update.message.reply_text(
            hdr + trail + body,
            parse_mode="HTML", reply_markup=kb,
        )
        return ASK_SL_TIMEFRAME

    @auth_required
    async def trade_sl_timeframe(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        timeframe = query.data.split(":")[1]
        d = context.user_data[TRADE_KEY]
        d["sl_timeframe"] = timeframe
        side = d["side"]
        tp_hint = "above entry" if side == "long" else "below entry"
        sl_label = f"${_fmt(d['sl'])}  ({timeframe})"

        hdr2  = f"<b>NEW TRADE</b>  /  Step 8 of 9\n<code>{"=" * 28}</code>\n\n"
        trail2 = _trail({'pair': d['pair'], 'side': d['_side_label'], 'sl': sl_label})
        body2  = (
            f"  <b>TAKE PROFIT 1</b>  —  {int(get_settings().tp1_pct*100)}% of position\n\n"
            f"  <i>Must be {tp_hint}.</i>"
        )
        kb_tp1 = InlineKeyboardMarkup([
            [InlineKeyboardButton("⏭  Skip — no TPs", callback_data="tp:skip_all")],
            [InlineKeyboardButton("✖  Abort trade",   callback_data="wizard:cancel")],
        ])
        await query.edit_message_text(
            hdr2 + trail2 + body2,
            parse_mode="HTML", reply_markup=kb_tp1,
        )
        return ASK_TP1

    @auth_required
    async def trade_tp1(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        try:
            tp1 = float(update.message.text.strip())
            if tp1 <= 0: raise ValueError
        except ValueError:
            await update.message.reply_text(
                "<b>INVALID PRICE</b>  Enter a number:", parse_mode="HTML", reply_markup=_abort_kb()
            )
            return ASK_TP1

        d    = context.user_data[TRADE_KEY]
        side = d.get("side")
        raw_entry = d.get("entry", 0)
        entry = float(raw_entry) if raw_entry and raw_entry != "market" else 0
        if entry > 0:
            if side == "long" and tp1 <= entry:
                await update.message.reply_text(
                    f"<b>INVALID TP1</b>  ${_fmt(tp1)} must be <b>above</b> entry ${_fmt(entry)} for LONG.",
                    parse_mode="HTML", reply_markup=_abort_kb()
                )
                return ASK_TP1
            if side == "short" and tp1 >= entry:
                await update.message.reply_text(
                    f"<b>INVALID TP1</b>  ${_fmt(tp1)} must be <b>below</b> entry ${_fmt(entry)} for SHORT.",
                    parse_mode="HTML", reply_markup=_abort_kb()
                )
                return ASK_TP1
        d["tp1"] = tp1
        s = get_settings()
        kb_tp2 = InlineKeyboardMarkup([
            [InlineKeyboardButton("⏭  Skip TP2 & TP3", callback_data="tp:skip_tp2")],
            [InlineKeyboardButton("✖  Abort trade",    callback_data="wizard:cancel")],
        ])
        await update.message.reply_text(
            f"<b>NEW TRADE</b>  /  Step 9 of 9\n"
            f"<code>{'═'*28}</code>\n\n"
            f"{_trail({'pair': d['pair'], 'side': d['_side_label'], 'tp1': f'${_fmt(tp1)}'})}"
            f"  <b>TAKE PROFIT 2</b>  —  {int(s.tp2_pct*100)}% of position",
            parse_mode="HTML", reply_markup=kb_tp2,
        )
        return ASK_TP2

    @auth_required
    async def trade_tp2(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        try:
            tp2 = float(update.message.text.strip())
            if tp2 <= 0: raise ValueError
        except ValueError:
            await update.message.reply_text(
                "<b>INVALID PRICE</b>  Enter a number:", parse_mode="HTML", reply_markup=_abort_kb()
            )
            return ASK_TP2

        d    = context.user_data[TRADE_KEY]
        side = d.get("side")
        tp1  = float(d.get("tp1", 0))
        if side == "long" and tp2 <= tp1:
            await update.message.reply_text(
                f"<b>INVALID TP2</b>  ${_fmt(tp2)} must be <b>above</b> TP1 ${_fmt(tp1)} for LONG.",
                parse_mode="HTML", reply_markup=_abort_kb()
            )
            return ASK_TP2
        if side == "short" and tp2 >= tp1:
            await update.message.reply_text(
                f"<b>INVALID TP2</b>  ${_fmt(tp2)} must be <b>below</b> TP1 ${_fmt(tp1)} for SHORT.",
                parse_mode="HTML", reply_markup=_abort_kb()
            )
            return ASK_TP2
        d["tp2"] = tp2
        s = get_settings()
        kb_tp3 = InlineKeyboardMarkup([
            [InlineKeyboardButton("⏭  Skip TP3", callback_data="tp:skip_tp3")],
            [InlineKeyboardButton("✖  Abort trade", callback_data="wizard:cancel")],
        ])
        await update.message.reply_text(
            f"<b>NEW TRADE</b>  /  Step 9 of 9\n"
            f"<code>{'═'*28}</code>\n\n"
            f"{_trail({'pair': d['pair'], 'side': d['_side_label'], 'tp2': f'${_fmt(tp2)}'})}"
            f"  <b>TAKE PROFIT 3</b>  —  {int(s.tp3_pct*100)}% of position\n\n"
            f"  <i>Final target. Remaining position closes here.</i>",
            parse_mode="HTML", reply_markup=kb_tp3,
        )
        return ASK_TP3

    @auth_required
    async def trade_tp3(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        try:
            tp3 = float(update.message.text.strip())
            if tp3 <= 0: raise ValueError
        except ValueError:
            await update.message.reply_text(
                "<b>INVALID PRICE</b>  Enter a number:", parse_mode="HTML", reply_markup=_abort_kb()
            )
            return ASK_TP3

        d    = context.user_data[TRADE_KEY]
        side = d.get("side")
        tp2  = float(d.get("tp2", 0))
        if side == "long" and tp3 <= tp2:
            await update.message.reply_text(
                f"<b>INVALID TP3</b>  ${_fmt(tp3)} must be <b>above</b> TP2 ${_fmt(tp2)} for LONG.",
                parse_mode="HTML", reply_markup=_abort_kb()
            )
            return ASK_TP3
        if side == "short" and tp3 >= tp2:
            await update.message.reply_text(
                f"<b>INVALID TP3</b>  ${_fmt(tp3)} must be <b>below</b> TP2 ${_fmt(tp2)} for SHORT.",
                parse_mode="HTML", reply_markup=_abort_kb()
            )
            return ASK_TP3
        d["tp3"] = tp3
        return await self._ask_dca(update.message, d, edit=False)

    @auth_required
    async def trade_dca(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        try:
            dca = float(update.message.text.strip())
            if dca <= 0: raise ValueError
        except ValueError:
            await update.message.reply_text(
                "<b>INVALID PRICE</b>  Enter a number or skip:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⏭  Skip", callback_data="dca:skip")],
                    [InlineKeyboardButton("✖  Abort trade", callback_data="wizard:cancel")],
                ]),
            )
            return ASK_DCA

        d     = context.user_data[TRADE_KEY]
        side  = d.get("side")
        raw_entry = d.get("entry", 0)
        entry = float(raw_entry) if raw_entry and raw_entry != "market" else 0
        if entry > 0:
            if side == "long" and dca >= entry:
                await update.message.reply_text(
                    f"<b>INVALID DCA</b>  ${_fmt(dca)} must be <b>below</b> entry ${_fmt(entry)} for LONG.",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("⏭  Skip", callback_data="dca:skip")],
                        [InlineKeyboardButton("✖  Abort trade", callback_data="wizard:cancel")],
                    ]),
                )
                return ASK_DCA
            if side == "short" and dca <= entry:
                await update.message.reply_text(
                    f"<b>INVALID DCA</b>  ${_fmt(dca)} must be <b>above</b> entry ${_fmt(entry)} for SHORT.",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("⏭  Skip", callback_data="dca:skip")],
                        [InlineKeyboardButton("✖  Abort trade", callback_data="wizard:cancel")],
                    ]),
                )
                return ASK_DCA
        context.user_data[TRADE_KEY]["dca"] = dca
        return await self._build_confirm(update, context, edit=False)

    @auth_required
    async def trade_tp_skip_all(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """User skipped all TPs — jump straight to DCA."""
        await update.callback_query.answer()
        d = context.user_data[TRADE_KEY]
        d["tp1"] = None
        d["tp2"] = None
        d["tp3"] = None
        return await self._ask_dca(update.callback_query, d, edit=True)

    @auth_required
    async def trade_tp2_skip(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """User skipped TP2 and TP3 — jump to DCA."""
        await update.callback_query.answer()
        d = context.user_data[TRADE_KEY]
        d["tp2"] = None
        d["tp3"] = None
        return await self._ask_dca(update.callback_query, d, edit=True)

    @auth_required
    async def trade_tp3_skip(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """User skipped TP3 — jump to DCA."""
        await update.callback_query.answer()
        d = context.user_data[TRADE_KEY]
        d["tp3"] = None
        return await self._ask_dca(update.callback_query, d, edit=True)

    async def _ask_dca(self, query_or_msg, d: dict, edit: bool) -> int:
        """Show the DCA prompt. Called after all TPs are set (or skipped)."""
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⏭  Skip — no DCA", callback_data="dca:skip")],
            [InlineKeyboardButton("✖  Abort trade",   callback_data="wizard:cancel")],
        ])
        text = (
            f"<b>NEW TRADE</b>  /  Optional\n"
            f"<code>{'═'*28}</code>\n\n"
            f"  <b>DCA ENTRY</b>  (Dollar Cost Average)\n\n"
            f"  Add a second limit entry at a better price,\n"
            f"  or tap Skip to go straight to the summary."
        )
        if edit and hasattr(query_or_msg, 'edit_message_text'):
            await query_or_msg.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        else:
            await query_or_msg.reply_text(text, parse_mode="HTML", reply_markup=kb)
        return ASK_DCA

    @auth_required
    async def trade_dca_skip(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        await update.callback_query.answer()
        context.user_data[TRADE_KEY]["dca"] = None
        return await self._build_confirm(update, context, edit=True)

    async def _build_confirm(self, update, context, edit: bool = False) -> int:
        d   = context.user_data[TRADE_KEY]
        s   = get_settings()
        msg = "⏳ Calculating position..."

        if edit and update.callback_query:
            await update.callback_query.edit_message_text(msg)
        else:
            sent = await update.message.reply_text(msg)

        try:
            # Store DCA multiplier — order_manager applies it to the computed position_size
            dca_price = d.get("dca")
            strategy  = d.get("strategy")  # "neil" | "saltwayer" | None
            if dca_price and strategy == "neil":
                d["dca_qty"] = 2.0   # multiplier
            elif dca_price and strategy == "saltwayer":
                d["dca_qty"] = 2.5   # multiplier
            else:
                d["dca_qty"] = None  # same size as entry

            req = TradeRequest(
                pair=d["pair"], side=Side(d["side"]), entry=d["entry"],
                risk_value=d["risk_value"],
                risk_type=RiskType.PERCENT if d["risk_type"] == "percent" else RiskType.DOLLAR,
                tp1=d.get("tp1") or None,
                tp2=d.get("tp2") or None,
                tp3=d.get("tp3") or None,
                sl=d["sl"],
                sl_timeframe=d.get("sl_timeframe", "1h"),
                dca=dca_price,
                strategy=strategy,
                dca_qty=d.get("dca_qty"),
            )
            sizing = await self._rm.calculate_position(req)
            self._rm.validate_tps(req, sizing.entry_price)
        except (ValidationError, InsufficientMarginError) as e:
            await self._jnl.log_rejection(d.get("pair", "?"), str(e))
            txt = M.validation_error(str(e))
            if edit and update.callback_query:
                await update.callback_query.edit_message_text(txt, parse_mode="HTML")
            else:
                await sent.edit_text(txt, parse_mode="HTML")
            context.user_data.pop(TRADE_KEY, None)
            return ConversationHandler.END
        except APIError as e:
            await self._jnl.log_error("Sizing API error", d.get("pair", "?"), e.response)
            txt = M.api_error("Position sizing", e)
            if edit and update.callback_query:
                await update.callback_query.edit_message_text(txt, parse_mode="HTML")
            else:
                await sent.edit_text(txt, parse_mode="HTML")
            context.user_data.pop(TRADE_KEY, None)
            return ConversationHandler.END

        d["_req"]    = req
        d["_sizing"] = sizing

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅  CONFIRM & PLACE", callback_data="trade:confirm"),
            InlineKeyboardButton("✖  Cancel",          callback_data="trade:cancel"),
        ]])
        txt = M.wizard_summary(req, sizing)

        if edit and update.callback_query:
            await update.callback_query.edit_message_text(txt, parse_mode="HTML", reply_markup=kb)
        else:
            await sent.edit_text(txt, parse_mode="HTML", reply_markup=kb)

        return CONFIRM_TRADE

    @auth_required
    async def trade_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        d      = context.user_data.get(TRADE_KEY, {})
        req    = d.get("_req")
        sizing = d.get("_sizing")

        if not req or not sizing:
            await query.edit_message_text(
                "<b>SESSION EXPIRED</b>\n\nUse /trade to start a new trade.",
                parse_mode="HTML",
            )
            return ConversationHandler.END

        await query.edit_message_text("⏳ Placing orders on Bitunix...")

        try:
            trade = await self._om.open_trade(req, sizing)
            # Register soft SL monitoring (no exchange order placed)
            try:
                await self._om.set_soft_sl(req.pair, req.sl, req.sl_timeframe)
            except Exception as e:
                logger.warning(f"Could not set soft SL after open: {e}")

            msg_text = M.trade_opened(trade, DEBUG_MODE)
            # Warn if TPs weren't placed (limit entry not yet filled)
            if not trade.tp1_order_id and not req.is_market:
                msg_text += (
                    "\n\n⚠️  <b>Limit entry not yet filled.</b>\n"
                    "  TP orders will be placed after fill.\n"
                    "  Run <code>/sync</code> once your entry fills."
                )
            await query.edit_message_text(msg_text, parse_mode="HTML")
        except DuplicateTradeError as e:
            await query.edit_message_text(
                f"<b>DUPLICATE BLOCKED</b>  ⚠️\n\n  {e}", parse_mode="HTML"
            )
        except APIError as e:
            await self._jnl.log_error("Order placement", req.pair, e.response)
            await query.edit_message_text(
                M.api_error("Order placement", e), parse_mode="HTML"
            )
        except Exception as e:
            await self._jnl.log_error("Unexpected", req.pair, str(e))
            await query.edit_message_text(
                f"<b>UNEXPECTED ERROR</b>\n\n<code>{e}</code>", parse_mode="HTML"
            )
        finally:
            context.user_data.pop(TRADE_KEY, None)
        return ConversationHandler.END

    @auth_required
    async def trade_cancel_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        await update.callback_query.answer()
        context.user_data.pop(TRADE_KEY, None)
        await update.callback_query.edit_message_text(
            "<b>TRADE CANCELLED</b>  ✖\n\n  No orders were placed.", parse_mode="HTML"
        )
        return ConversationHandler.END

    async def _wiz_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        await update.callback_query.answer()
        context.user_data.pop(TRADE_KEY, None)
        await update.callback_query.edit_message_text(
            "<b>TRADE CANCELLED</b>  ✖\n\n  No orders were placed.", parse_mode="HTML"
        )
        return ConversationHandler.END

    @auth_required
    async def _cmd_cancel_wizard(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        context.user_data.pop(TRADE_KEY, None)
        await update.message.reply_text(
            "<b>TRADE CANCELLED</b>  ✖\n\n  Wizard aborted.", parse_mode="HTML"
        )
        return ConversationHandler.END

    # ── Info commands ─────────────────────────────────────────────────────────

    async def _notify(self, text: str) -> None:
        """Send a message to the authorised user's chat."""
        if self._chat_id and self._app:
            try:
                await self._app.bot.send_message(
                    chat_id=self._chat_id, text=text, parse_mode="HTML"
                )
            except Exception as e:
                logger.error(f"_notify failed: {e}")
        else:
            logger.warning("_notify: no chat_id or app available")

    def _capture_chat(self, update: Update) -> None:
        """Keep chat_id current (already pre-seeded from AUTHORIZED_USER_ID)."""
        if update and update.effective_chat:
            self._chat_id = update.effective_chat.id

    @auth_required
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._capture_chat(update)
        await update.message.reply_text(M.start(DEBUG_MODE), parse_mode="HTML")

    @auth_required
    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(M.help_msg(), parse_mode="HTML")

    @auth_required
    async def cmd_commands(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(M.commands_msg(DEBUG_MODE), parse_mode="HTML")

    # ── Account commands ──────────────────────────────────────────────────────

    @auth_required
    async def cmd_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = await update.message.reply_text("⏳ Fetching balance...")
        try:
            avail  = await self._client.get_balance()
            equity = await self._client.get_total_balance()
            await msg.edit_text(M.balance(avail, equity), parse_mode="HTML")
        except APIError as e:
            await msg.edit_text(M.api_error("Get balance", e), parse_mode="HTML")
            logger.error(f"cmd_balance: {e}")

    @auth_required
    async def cmd_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = await update.message.reply_text("⏳ Fetching positions...")
        try:
            pos_list      = await self._client.get_positions()
            active_trades = await self._db.get_active_trades()
            # Classify live pending orders per position for accurate TP/DCA/SL display.
            # On Bybit, SL lives in position data (stop_loss field), not a separate order.
            classified: dict[str, dict] = {}
            for pos in pos_list:
                is_long = pos.side.upper() == "LONG"
                result = await self._om.classify_orders(
                    pos.symbol, pos.entry_price, is_long
                )
                # Inject native SL so messages.py can show it even without a bot trade record
                if pos.stop_loss and pos.stop_loss > 0:
                    result["native_sl"] = pos.stop_loss
                classified[pos.symbol] = result
            await msg.edit_text(
                M.positions(pos_list, active_trades, classified),
                parse_mode="HTML"
            )
        except APIError as e:
            await msg.edit_text(M.api_error("Get positions", e), parse_mode="HTML")
            logger.error(f"cmd_positions: {e}")

    @auth_required
    async def cmd_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        trades = await self._db.get_closed_trades(limit=10)
        await update.message.reply_text(M.history(trades), parse_mode="HTML")

    @auth_required
    async def cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        s = await self._db.get_stats()
        await update.message.reply_text(M.stats(s or {}), parse_mode="HTML")

    # ── Management commands ───────────────────────────────────────────────────

    @auth_required
    async def cmd_cancelpair(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args:
            await update.message.reply_text(
                "<b>USAGE</b>\n\n  <code>/cancelpair BTCUSDT</code>", parse_mode="HTML"
            )
            return
        pair = context.args[0].upper()
        msg  = await update.message.reply_text(f"⏳ Cancelling orders for {pair}...")
        try:
            await self._om.cancel_pair(pair)
            await msg.edit_text(
                f"<b>ORDERS CANCELLED</b>  ✅\n"
                f"<code>{'─'*28}</code>\n\n"
                f"  All open orders for <b>{pair}</b> cancelled.\n"
                f"  <i>The position itself (if any) is still open.</i>",
                parse_mode="HTML",
            )
        except APIError as e:
            await msg.edit_text(M.api_error(f"Cancel orders for {pair}", e), parse_mode="HTML")

    @auth_required
    async def cmd_closeall(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        s      = get_settings()
        active = await self._db.get_active_trades()
        count  = len(active)
        pairs  = ", ".join(active.keys()) if active else "none tracked"

        if not s.close_all_confirmation:
            # Skip confirmation if disabled in settings
            await update.message.reply_text("⏳ Closing all positions...")
            await self._do_close_all(update)
            return

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅  YES — CLOSE ALL", callback_data="closeall:confirm"),
            InlineKeyboardButton("✖  Cancel",           callback_data="closeall:cancel"),
        ]])
        await update.message.reply_text(
            f"<b>CLOSE ALL POSITIONS</b>  🚨\n"
            f"<code>{'═'*28}</code>\n\n"
            f"  This will <b>market close every open position</b>\n"
            f"  and cancel all pending orders.\n\n"
            f"  <code>{'Tracked positions':<18}</code>  {count}\n"
            f"  <code>{'Pairs':<18}</code>  {pairs}\n\n"
            f"<code>{'─'*28}</code>\n"
            f"  ⚠️ <b>This cannot be undone.</b>",
            parse_mode="HTML", reply_markup=kb,
        )

    async def _do_close_all(self, update_or_query) -> None:
        try:
            closed = await self._om.close_all()
            if closed:
                pairs     = ", ".join(t.pair for t in closed)
                total_pnl = sum(t.realized_pnl for t in closed)
                sign      = "+" if total_pnl >= 0 else ""
                text      = (
                    f"<b>ALL POSITIONS CLOSED</b>  ✅\n"
                    f"<code>{'─'*28}</code>\n\n"
                    f"  <code>{'Pairs closed':<16}</code>  {pairs}\n"
                    f"  <code>{'Total PnL':<16}</code>  {sign}${total_pnl:,.2f}"
                )
            else:
                text = "<b>DONE</b>  ✅\n\n  No tracked positions were open."

            if hasattr(update_or_query, "edit_message_text"):
                await update_or_query.edit_message_text(text, parse_mode="HTML")
            else:
                await update_or_query.message.reply_text(text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"close_all error: {e}")
            err = f"<b>CLOSE ALL FAILED</b>  ❌\n\n<code>{e}</code>"
            if hasattr(update_or_query, "edit_message_text"):
                await update_or_query.edit_message_text(err, parse_mode="HTML")
            else:
                await update_or_query.message.reply_text(err, parse_mode="HTML")

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        if query.data == "closeall:confirm":
            await query.edit_message_text("⏳ Closing all positions...")
            await self._do_close_all(query)
        elif query.data == "closeall:cancel":
            await query.edit_message_text(
                "<b>CANCELLED</b>  ✖\n\n  No positions were closed.", parse_mode="HTML"
            )

    @auth_required
    async def cmd_sync(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = await update.message.reply_text("⏳ Scanning exchange for untracked positions...")
        try:
            imported = await self._om.sync_from_exchange()
            if not imported:
                await msg.edit_text(
                    "<b>SYNC COMPLETE</b>  ✅\n"
                    "<code>────────────────────────────</code>\n\n"
                    "  All open positions are already tracked.\n"
                    "  No new trades imported.",
                    parse_mode="HTML",
                )
            else:
                lines = [f"<b>SYNC COMPLETE</b>  ✅  ·  {len(imported)} imported\n<code>════════════════════════════</code>\n"]
                for t in imported:
                    side_e  = "🟢" if t.side == Side.LONG else "🔴"
                    sl_str  = f"${_fmt(t.sl)}"  if t.sl  else "—"
                    tp1_str = f"${_fmt(t.tp1)}" if t.tp1 else "—"
                    tp3_str = f"${_fmt(t.tp3)}" if t.tp3 else "—"
                    lines.append(
                        f"\n  <b>{t.pair}</b>  {side_e}  ·  {t.position_size} @ ${_fmt(t.entry)}\n"
                        f"  <code>{'ID':<10}</code>  <code>{t.trade_id}</code>\n"
                        f"  <code>{'SL':<10}</code>  {sl_str}\n"
                        f"  <code>{'TP1 / TP3':<10}</code>  {tp1_str} / {tp3_str}"
                    )
                lines.append("\n<code>────────────────────────────</code>\n  <i>Use /modifysl PAIR PRICE to adjust any SL.</i>")
                await msg.edit_text("\n".join(lines), parse_mode="HTML")
        except APIError as e:
            logger.error(f"cmd_sync APIError: {e}")
            await msg.edit_text(M.api_error("Sync from exchange", e), parse_mode="HTML")
        except Exception as e:
            logger.error(f"cmd_sync unexpected error: {e}", exc_info=True)
            await msg.edit_text(
                f"<b>SYNC FAILED</b>  ⚠️\n\n<code>{e}</code>",
                parse_mode="HTML",
            )

    @auth_required
    async def cmd_resync(self, update, context):
        args = context.args or []
        sep = "─" * 28
        if not args:
            await update.message.reply_text(
                "<b>RESYNC PAIR</b>\n"
                f"<code>{sep}</code>\n\n"
                "  Drops a pair from tracking and re-imports\n"
                "  it fresh from the exchange.\n\n"
                "  Use when TP/SL data is stale or wrong.\n"
                "  <b>Does not touch positions or orders.</b>\n\n"
                "  <b>Usage:</b>  <code>/resync BTCUSDT</code>",
                parse_mode="HTML",
            )
            return

        pair = args[0].upper()
        msg  = await update.message.reply_text(
            f"⏳ Resyncing <b>{pair}</b>...", parse_mode="HTML"
        )
        try:
            dropped = await self._om.drop_trade(pair)
            if not dropped:
                await msg.edit_text(
                    f"<b>NOT FOUND</b>\n\n"
                    f"  <b>{pair}</b> is not currently tracked.\n"
                    f"  Running a full sync anyway...",
                    parse_mode="HTML",
                )

            imported = await self._om.sync_from_exchange()
            match = next((t for t in imported if t.pair == pair), None)

            if match:
                side_e = "🟢" if match.side == Side.LONG else "🔴"
                tp1_s  = f"${_fmt(match.tp1)}" if match.tp1 else "—"
                tp2_s  = f"${_fmt(match.tp2)}" if match.tp2 else "—"
                tp3_s  = f"${_fmt(match.tp3)}" if match.tp3 else "—"
                sl_s   = f"${_fmt(match.sl)}"  if match.sl  else "—"
                await msg.edit_text(
                    f"<b>RESYNC COMPLETE</b>  ✅\n<code>{sep}</code>\n\n"
                    f"  <b>{pair}</b>  {side_e}  ·  {match.position_size} @ ${_fmt(match.entry)}\n\n"
                    f"  <code>{'ID':<10}</code>  <code>{match.trade_id}</code>\n"
                    f"  <code>{'TP1/2/3':<10}</code>  {tp1_s}  {tp2_s}  {tp3_s}\n"
                    f"  <code>{'Stop Loss':<10}</code>  {sl_s}",
                    parse_mode="HTML",
                )
            else:
                await msg.edit_text(
                    f"<b>RESYNC</b>  ⚠️\n\n"
                    f"  <b>{pair}</b> was dropped from tracking\n"
                    f"  but no live position found on exchange.\n\n"
                    f"  <i>The position may have already closed.</i>",
                    parse_mode="HTML",
                )
        except APIError as e:
            await msg.edit_text(M.api_error("Resync", e), parse_mode="HTML")
        except Exception as e:
            logger.error(f"cmd_resync: {e}", exc_info=True)
            await msg.edit_text(f"<b>RESYNC FAILED</b>\n\n<code>{e}</code>", parse_mode="HTML")

    @auth_required
    async def cmd_modifysl(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args or len(context.args) < 2:
            await update.message.reply_text(
                "<b>USAGE</b>\n\n  <code>/modifysl BTCUSDT 93000</code>",
                parse_mode="HTML",
            )
            return
        pair = context.args[0].upper()
        try:
            new_sl = float(context.args[1])
        except ValueError:
            await update.message.reply_text("<b>INVALID PRICE</b>  Enter a number.", parse_mode="HTML")
            return
        msg = await update.message.reply_text(f"⏳ Moving SL for {pair} to ${_fmt(new_sl)}...")
        try:
            trade = self._om.get_active_trade(pair)
            old_sl = await self._om.modify_sl(pair, new_sl)
            is_be  = trade and abs(new_sl - trade.entry) < 1e-9
            if trade:
                if is_be:
                    await self._jnl.log_sl_moved_to_be(trade, old_sl)
                else:
                    await self._jnl.log_sl_modified(trade, old_sl, new_sl)
            sep = '─' * 28
            be_note = "  <i>(breakeven)</i>" if is_be else ""
            await msg.edit_text(
                f"<b>STOP LOSS UPDATED</b>  ✅\n"
                f"<code>{sep}</code>\n\n"
                f"  <b>{pair}</b>\n"
                f"  <code>Old SL  </code>  ${_fmt(old_sl)}\n"
                f"  <code>New SL  </code>  ${_fmt(new_sl)}{be_note}",
                parse_mode="HTML",
            )
        except ValueError as e:
            await msg.edit_text(f"<b>ERROR</b>  {e}", parse_mode="HTML")
        except APIError as e:
            await msg.edit_text(M.api_error(f"Modify SL {pair}", e), parse_mode="HTML")

    # ── Soft SL breach alert ──────────────────────────────────────────────────

    # ── Soft SL breach alert ──────────────────────────────────────────────────

    async def _on_soft_sl_breach(
        self,
        trade,
        timeframe: str,
        close_price: float,
        candle_ts: int,
    ) -> None:
        """
        Called by SoftSLMonitor when a candle closes beyond the soft SL.
        Sends an alert with Close / Ignore inline buttons.
        """
        from models import Side
        side_tag  = "🟢 LONG" if trade.side == Side.LONG else "🔴 SHORT"
        direction = "below" if trade.side == Side.LONG else "above"
        sep       = "═" * 28
        lbl_close = "Candle close"
        lbl_sl    = "Soft SL"
        lbl_tf    = "Timeframe"
        lbl_entry = "Entry"

        # Always fire Discord webhook — independent of Telegram chat_id
        await self._jnl.log_soft_sl_breach(trade, timeframe, close_price)

        if not self._chat_id or not self._app:
            logger.warning("SoftSL breach: no chat_id — webhook fired but Telegram alert skipped")
            return

        text = (
            f"⚠️  <b>SOFT SL BREACHED</b>\n"
            f"<code>{sep}</code>\n\n"
            f"  <b>{trade.pair}</b>  {side_tag}\n\n"
            f"  <code>{lbl_close:<14}</code>  <b>${_fmt(close_price)}</b>\n"
            f"  <code>{lbl_sl:<14}</code>  ${_fmt(trade.soft_sl_price)}\n"
            f"  <code>{lbl_tf:<14}</code>  {timeframe}\n"
            f"  <code>{lbl_entry:<14}</code>  ${_fmt(trade.entry)}\n\n"
            f"  The {timeframe} candle closed <b>{direction}</b> your soft SL.\n"
            f"  <i>No position has been closed — your call.</i>"
        )

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "🔴  CLOSE POSITION",
                callback_data=f"softsl:close:{trade.pair}:{candle_ts}",
            ),
            InlineKeyboardButton(
                "⏩  IGNORE",
                callback_data=f"softsl:ignore:{trade.pair}:{timeframe}:{candle_ts}",
            ),
        ]])

        try:
            await self._app.bot.send_message(
                chat_id=self._chat_id,
                text=text,
                parse_mode="HTML",
                reply_markup=kb,
            )
            logger.info(f"SoftSL alert sent: {trade.pair} {timeframe} close={close_price}")
        except Exception as e:
            logger.error(f"SoftSL alert send failed: {e}")

    async def _soft_sl_action(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle Close / Ignore buttons from soft SL breach alerts."""
        query = update.callback_query
        await query.answer()
        parts  = query.data.split(":")
        action = parts[1]
        pair   = parts[2]

        if action == "close":
            await query.edit_message_text(f"⏳ Closing {pair}...")
            try:
                trade = await self._om.close_trade(pair, reason="soft_sl")
                if trade:
                    self._soft_sl_monitor.clear_cooldown(pair)
                    sep = "─" * 28
                    await query.edit_message_text(
                        f"<b>POSITION CLOSED</b>  ✅\n"
                        f"<code>{sep}</code>\n\n"
                        f"  <b>{pair}</b> closed via soft SL alert.\n"
                        f"  Check /history for final PnL.",
                        parse_mode="HTML",
                    )
                else:
                    await query.edit_message_text(
                        f"<b>NOT FOUND</b>  {pair} may have already been closed.",
                        parse_mode="HTML",
                    )
            except Exception as e:
                logger.error(f"soft_sl close error: {e}")
                await query.edit_message_text(
                    f"<b>CLOSE FAILED</b>\n\n<code>{e}</code>",
                    parse_mode="HTML",
                )

        elif action == "ignore":
            timeframe = parts[3]
            candle_ts = int(parts[4])
            self._soft_sl_monitor.notify_cooldown_ignored(pair, timeframe, candle_ts)
            sep = "─" * 28
            await query.edit_message_text(
                f"<b>IGNORED</b>  ⏩\n"
                f"<code>{sep}</code>\n\n"
                f"  Alert for <b>{pair}</b> dismissed.\n"
                f"  No re-alert for 2 more {timeframe} candles.\n\n"
                f"  Use <code>/setsl {pair} PRICE {timeframe}</code> to update the level.",
                parse_mode="HTML",
            )
            trade = self._om.get_active_trade(pair)
            if trade:
                await self._jnl.log_soft_sl_ignored(trade, timeframe)

    # ── /setstrategy command ──────────────────────────────────────────────────

    @auth_required
    async def cmd_setstrategy(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        Tag a tracked trade with a strategy label.
        Usage:
          /setstrategy PAIR neil
          /setstrategy PAIR saltwayer
          /setstrategy PAIR none
        """
        args = context.args or []
        sep  = "─" * 28

        if len(args) < 2:
            await update.message.reply_text(
                f"<b>SET STRATEGY</b>\n"
                f"<code>{sep}</code>\n\n"
                f"  <code>/setstrategy PAIR neil</code>\n"
                f"  <code>/setstrategy PAIR saltwayer</code>\n"
                f"  <code>/setstrategy PAIR none</code>",
                parse_mode="HTML",
            )
            return

        pair     = args[0].upper()
        strategy = args[1].lower()

        if strategy not in ("neil", "saltwayer", "none"):
            await update.message.reply_text(
                f"<b>INVALID STRATEGY</b>\n\n"
                f"  Valid options: <code>neil</code>  <code>saltwayer</code>  <code>none</code>",
                parse_mode="HTML",
            )
            return

        trade = self._om.get_active_trade(pair)
        if not trade:
            await update.message.reply_text(
                f"<b>NOT FOUND</b>  No active trade for <b>{pair}</b>.\n\n"
                f"  Use /sync to import it first.",
                parse_mode="HTML",
            )
            return

        trade.strategy = None if strategy == "none" else strategy
        await self._db.update_trade(trade)
        self._om._cache[pair] = trade

        if strategy == "none":
            await update.message.reply_text(
                f"<b>STRATEGY CLEARED</b>  ✅\n\n"
                f"  <b>{pair}</b>  strategy label removed.",
                parse_mode="HTML",
            )
        else:
            labels = {"neil": "📐 Neil", "saltwayer": "🌊 Saltwayer"}
            await update.message.reply_text(
                f"<b>STRATEGY SET</b>  ✅\n"
                f"<code>{sep}</code>\n\n"
                f"  <b>{pair}</b>  →  {labels[strategy]}",
                parse_mode="HTML",
            )

    # ── /movesl command ───────────────────────────────────────────────────────

    @auth_required
    async def cmd_movesl(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        Move SL on the exchange.
        Usage:
          /movesl BTCUSDT be        — move to breakeven (entry price)
          /movesl BTCUSDT 93000     — move to specific price
        """
        args = context.args or []
        if len(args) < 2:
            await update.message.reply_text(
                f"<b>MOVE STOP LOSS</b>\n"
                f"<code>{'─'*28}</code>\n\n"
                f"  <code>/movesl PAIR be</code>       — move to breakeven\n"
                f"  <code>/movesl PAIR PRICE</code>    — move to specific price",
                parse_mode="HTML",
            )
            return

        pair = args[0].upper()
        trade = self._om.get_active_trade(pair)
        if not trade:
            await update.message.reply_text(
                f"<b>NOT FOUND</b>  No active trade for <b>{pair}</b>.\n\n"
                f"  Use /sync to import it first.",
                parse_mode="HTML",
            )
            return

        raw = args[1].lower()
        if raw == "be":
            new_sl = trade.entry
            label  = f"${_fmt(new_sl)}  <i>(breakeven)</i>"
        else:
            try:
                new_sl = float(raw)
                label  = f"${_fmt(new_sl)}"
            except ValueError:
                await update.message.reply_text(
                    "<b>INVALID PRICE</b>  Use a number or <code>be</code> for breakeven.",
                    parse_mode="HTML",
                )
                return

        msg = await update.message.reply_text(f"⏳ Moving SL for {pair}...")
        try:
            old_sl = await self._om.modify_sl(pair, new_sl)
            is_be  = abs(new_sl - trade.entry) < 1e-9
            if is_be:
                await self._jnl.log_sl_moved_to_be(trade, old_sl)
            else:
                await self._jnl.log_sl_modified(trade, old_sl, new_sl)
            sep = "─" * 28
            await msg.edit_text(
                f"<b>SL MOVED</b>  ✅\n"
                f"<code>{sep}</code>\n\n"
                f"  <b>{pair}</b>\n"
                f"  <code>Old SL  </code>  ${_fmt(old_sl)}\n"
                f"  <code>New SL  </code>  {label}",
                parse_mode="HTML",
            )
        except ValueError as e:
            await msg.edit_text(f"<b>ERROR</b>  {e}", parse_mode="HTML")
        except APIError as e:
            await msg.edit_text(M.api_error(f"Move SL {pair}", e), parse_mode="HTML")

    # ── /setsl command ────────────────────────────────────────────────────────

    @auth_required
    async def cmd_setsl(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        Set or update the soft SL for a tracked trade.
        Usage:
          /setsl BTCUSDT 93000 1h      — set SL level + timeframe
          /setsl BTCUSDT 93000 Daily   — use daily candle close
          /setsl BTCUSDT off           — disable monitoring
        """
        args = context.args or []
        valid_tfs = list(PERIOD_SECONDS.keys())

        if len(args) < 2:
            tfs = "  ".join(valid_tfs)
            await update.message.reply_text(
                f"<b>SET SOFT STOP LOSS</b>\n"
                f"<code>{'─' * 28}</code>\n\n"
                f"  <b>Set:</b>      <code>/setsl PAIR PRICE TIMEFRAME</code>\n"
                f"  <b>Disable:</b>  <code>/setsl PAIR off</code>\n\n"
                f"  <b>Timeframes:</b>  <code>{tfs}</code>\n\n"
                f"  <i>No order is placed on the exchange.\n"
                f"  The bot watches candle closes and alerts you.</i>",
                parse_mode="HTML",
            )
            return

        pair = args[0].upper()

        # Disable
        if args[1].lower() == "off":
            try:
                await self._om.clear_soft_sl(pair)
                self._soft_sl_monitor.clear_cooldown(pair)
                await update.message.reply_text(
                    f"<b>SOFT SL DISABLED</b>  ✅\n\n"
                    f"  Candle monitoring off for <b>{pair}</b>.",
                    parse_mode="HTML",
                )
            except Exception as e:
                await update.message.reply_text(f"<b>ERROR</b>  {e}", parse_mode="HTML")
            return

        # Set
        if len(args) < 3:
            await update.message.reply_text(
                "<b>USAGE</b>  <code>/setsl PAIR PRICE TIMEFRAME</code>",
                parse_mode="HTML",
            )
            return

        try:
            price = float(args[1])
        except ValueError:
            await update.message.reply_text("<b>INVALID PRICE</b>  Enter a number.", parse_mode="HTML")
            return

        timeframe = args[2]
        if timeframe not in valid_tfs:
            tfs = "  ".join(valid_tfs)
            await update.message.reply_text(
                f"<b>INVALID TIMEFRAME</b>\n\nValid options:  <code>{tfs}</code>",
                parse_mode="HTML",
            )
            return

        # Validate SL direction against the tracked trade if we have one
        trade = self._om.get_active_trade(pair)
        if trade and trade.entry > 0:
            if trade.side == Side.LONG and price >= trade.entry:
                await update.message.reply_text(
                    f"<b>INVALID SL</b>  ${_fmt(price)} must be <b>below</b> "
                    f"entry ${_fmt(trade.entry)} for a LONG.\n\n"
                    f"  <i>If this is wrong, run /resync {pair} first.</i>",
                    parse_mode="HTML",
                )
                return
            if trade.side == Side.SHORT and price <= trade.entry:
                await update.message.reply_text(
                    f"<b>INVALID SL</b>  ${_fmt(price)} must be <b>above</b> "
                    f"entry ${_fmt(trade.entry)} for a SHORT.\n\n"
                    f"  <i>If this is wrong, run /resync {pair} first.</i>",
                    parse_mode="HTML",
                )
                return

        try:
            await self._om.set_soft_sl(pair, price, timeframe)
            self._soft_sl_monitor.clear_cooldown(pair)
            sep = "─" * 28
            await update.message.reply_text(
                f"<b>SOFT SL SET</b>  ✅\n"
                f"<code>{sep}</code>\n\n"
                f"  <b>{pair}</b>  →  <code>${_fmt(price)}</code>  ({timeframe})\n\n"
                f"  Alert fires when a <b>{timeframe} candle closes</b>\n"
                f"  beyond this level. No exchange order placed.",
                parse_mode="HTML",
            )
        except ValueError as e:
            await update.message.reply_text(f"<b>ERROR</b>  {e}", parse_mode="HTML")

    @auth_required
    async def cmd_setleverage(self, update, context):
        """Set leverage for a symbol — calls exchange and updates settings."""
        args = context.args or []
        sep = "────────────────────────────"
        if len(args) < 2:
            await update.message.reply_text(
                f"<b>SET LEVERAGE</b>\n<code>{sep}</code>\n\n"
                f"  <b>Usage:</b>  <code>/setleverage PAIR LEVERAGE</code>\n\n"
                f"  <b>Example:</b>  <code>/setleverage BTCUSDT 50</code>\n\n"
                f"  Sets leverage on the exchange and updates\n"
                f"  your configured max in /settings.\n\n"
                f"  <i>Capped at the exchange maximum per symbol.</i>",
                parse_mode="HTML",
            )
            return

        pair = args[0].upper()
        try:
            leverage = int(args[1])
            if leverage < 1:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "<b>INVALID</b>  Enter a whole number, e.g. <code>/setleverage BTCUSDT 50</code>",
                parse_mode="HTML",
            )
            return

        msg = await update.message.reply_text(
            f"⏳ Setting leverage for <b>{pair}</b>...", parse_mode="HTML"
        )
        try:
            symbol_max = await self._client.get_max_leverage(pair)
            capped     = min(leverage, symbol_max)
            await self._client.set_leverage(pair, capped)

            from settings_handler import get_settings
            s = get_settings()
            s.max_leverage = capped
            s.save()

            cap_note = (
                f"\n\n  <i>Requested {leverage}× — {pair} max is {symbol_max}×. Applied {capped}× instead.</i>"
                if capped != leverage else ""
            )
            await msg.edit_text(
                f"<b>LEVERAGE SET</b>  ✅\n<code>{sep}</code>\n\n"
                f"  <b>{pair}</b>  →  <code>{capped}×</code>{cap_note}\n\n"
                f"  Settings saved — all future trades on\n"
                f"  this pair will use <code>{capped}×</code> leverage.",
                parse_mode="HTML",
            )
        except APIError as e:
            await msg.edit_text(M.api_error("Set leverage", e), parse_mode="HTML")
        except Exception as e:
            logger.error(f"cmd_setleverage: {e}", exc_info=True)
            await msg.edit_text(f"<b>ERROR</b>  <code>{e}</code>", parse_mode="HTML")


    # ── /tp — add a TP order to a tracked trade ───────────────────────────────

    @auth_required
    async def cmd_tp(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        Add a take-profit limit order to a tracked trade.
        Usage:
          /tp PAIR PRICE QTY   — limit TP at PRICE for QTY tokens
          /tp PAIR cmp QTY     — market close QTY tokens now
        """
        sep  = "─" * 28
        args = context.args or []

        if len(args) < 2:
            await update.message.reply_text(
                f"<b>ADD TAKE PROFIT</b>\n"
                f"<code>{sep}</code>\n\n"
                f"  <code>/tp PAIR PRICE QTY</code>   — limit TP\n"
                f"  <code>/tp PAIR cmp QTY</code>     — market close now\n\n"
                f"  <b>QTY</b> is the number of tokens to close at this TP.",
                parse_mode="HTML",
            )
            return

        pair = args[0].upper()
        trade = self._om.get_active_trade(pair)
        if not trade:
            await update.message.reply_text(
                f"<b>NOT FOUND</b>  No active trade for <b>{pair}</b>.\n\n"
                f"  Use /sync to import it first.",
                parse_mode="HTML",
            )
            return

        price_raw = args[1].lower()
        is_market = price_raw in ("cmp", "market", "0")
        if is_market:
            price = 0.0
        else:
            try:
                price = float(price_raw)
                if price <= 0:
                    raise ValueError
            except ValueError:
                await update.message.reply_text(
                    "<b>INVALID PRICE</b>  Use a number or <code>cmp</code> for market.",
                    parse_mode="HTML",
                )
                return

        if not is_market and trade.entry > 0:
            if trade.side == Side.LONG and price <= trade.entry:
                await update.message.reply_text(
                    f"<b>INVALID TP</b>  ${_fmt(price)} must be <b>above</b> entry "
                    f"${_fmt(trade.entry)} for a LONG.",
                    parse_mode="HTML",
                )
                return
            if trade.side == Side.SHORT and price >= trade.entry:
                await update.message.reply_text(
                    f"<b>INVALID TP</b>  ${_fmt(price)} must be <b>below</b> entry "
                    f"${_fmt(trade.entry)} for a SHORT.",
                    parse_mode="HTML",
                )
                return

        if len(args) >= 3:
            try:
                qty = float(args[2])
                if qty <= 0:
                    raise ValueError
            except ValueError:
                await update.message.reply_text(
                    "<b>INVALID QTY</b>  Enter a positive number of tokens.",
                    parse_mode="HTML",
                )
                return
        else:
            await update.message.reply_text(
                f"<b>ADD TP — {pair}</b>\n"
                f"<code>{sep}</code>\n\n"
                f"  <code>Position size </code>  {trade.position_size} tokens\n"
                f"  <code>TP1 order     </code>  {'✅ set' if trade.tp1_order_id else '—'}\n"
                f"  <code>TP2 order     </code>  {'✅ set' if trade.tp2_order_id else '—'}\n"
                f"  <code>TP3 order     </code>  {'✅ set' if trade.tp3_order_id else '—'}\n\n"
                f"  Re-send with qty:\n"
                f"  <code>/tp {pair} {price_raw} QTY</code>",
                parse_mode="HTML",
            )
            return

        price_label = "CMP (market)" if is_market else f"${_fmt(price)}"
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("TP1", callback_data=f"addtp:{pair}:{price}:{qty}:1"),
                InlineKeyboardButton("TP2", callback_data=f"addtp:{pair}:{price}:{qty}:2"),
                InlineKeyboardButton("TP3", callback_data=f"addtp:{pair}:{price}:{qty}:3"),
            ],
            [InlineKeyboardButton("✖  Cancel", callback_data="addtp:cancel")],
        ])
        await update.message.reply_text(
            f"<b>ADD TAKE PROFIT — {pair}</b>\n"
            f"<code>{sep}</code>\n\n"
            f"  <code>Price  </code>  {price_label}\n"
            f"  <code>Qty    </code>  {qty} tokens\n\n"
            f"  Which TP slot?",
            parse_mode="HTML",
            reply_markup=kb,
        )

    @auth_required
    async def _handle_addtp(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        data = query.data

        if data == "addtp:cancel":
            await query.edit_message_text("<b>TP ORDER CANCELLED</b>", parse_mode="HTML")
            return

        try:
            _, pair, price_s, qty_s, slot_s = data.split(":")
            price = float(price_s)
            qty   = float(qty_s)
            slot  = int(slot_s)
        except Exception:
            await query.edit_message_text("<b>ERROR</b>  Malformed callback data.", parse_mode="HTML")
            return

        sep = "─" * 28
        await query.edit_message_text(f"⏳ Placing TP{slot} for {pair}...")
        try:
            order_id    = await self._om.add_tp_order(pair, price, qty, slot)
            price_label = "CMP (market)" if price == 0.0 else f"${_fmt(price)}"
            await query.edit_message_text(
                f"<b>TP{slot} PLACED</b>  ✅\n"
                f"<code>{sep}</code>\n\n"
                f"  <b>{pair}</b>\n"
                f"  <code>Slot   </code>  TP{slot}\n"
                f"  <code>Price  </code>  {price_label}\n"
                f"  <code>Qty    </code>  {qty} tokens\n"
                f"  <code>Order  </code>  <code>{order_id}</code>",
                parse_mode="HTML",
            )
        except (ValueError, APIError) as e:
            await query.edit_message_text(
                f"<b>FAILED TO PLACE TP{slot}</b>\n\n<code>{e}</code>",
                parse_mode="HTML",
            )

    # ── /dca — add DCA entry with independent risk sizing ─────────────────────

    @auth_required
    async def cmd_dca(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        Add a DCA entry to a tracked trade.
        Usage: /dca PAIR PRICE RISK NEW_SL
        """
        sep  = "─" * 28
        args = context.args or []

        if len(args) < 4:
            await update.message.reply_text(
                f"<b>ADD DCA</b>\n"
                f"<code>{sep}</code>\n\n"
                f"  <code>/dca PAIR PRICE RISK NEW_SL</code>\n\n"
                f"  <b>PRICE</b>   — DCA limit entry price\n"
                f"  <b>RISK</b>    — additional risk  <code>3%</code> or <code>150$</code>\n"
                f"  <b>NEW_SL</b>  — new combined stop loss price\n\n"
                f"  <i>Bot calculates token qty from RISK ÷ (PRICE − NEW_SL)\n"
                f"  and moves your exchange SL to NEW_SL.</i>",
                parse_mode="HTML",
            )
            return

        pair = args[0].upper()
        trade = self._om.get_active_trade(pair)
        if not trade:
            await update.message.reply_text(
                f"<b>NOT FOUND</b>  No active trade for <b>{pair}</b>.\n\n"
                f"  Use /sync to import it first.",
                parse_mode="HTML",
            )
            return

        if trade.dca_order_id:
            await update.message.reply_text(
                f"<b>DCA ALREADY SET</b>  {pair} already has a pending DCA order.\n\n"
                f"  Cancel it with <code>/cancelpair {pair}</code> first.",
                parse_mode="HTML",
            )
            return

        try:
            dca_price = float(args[1])
            if dca_price <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("<b>INVALID PRICE</b>  Enter a positive number.", parse_mode="HTML")
            return

        if trade.entry > 0:
            if trade.side == Side.LONG and dca_price >= trade.entry:
                await update.message.reply_text(
                    f"<b>INVALID DCA</b>  ${_fmt(dca_price)} must be <b>below</b> entry ${_fmt(trade.entry)} for LONG.",
                    parse_mode="HTML",
                )
                return
            if trade.side == Side.SHORT and dca_price <= trade.entry:
                await update.message.reply_text(
                    f"<b>INVALID DCA</b>  ${_fmt(dca_price)} must be <b>above</b> entry ${_fmt(trade.entry)} for SHORT.",
                    parse_mode="HTML",
                )
                return

        try:
            risk_value, risk_type_str = parse_risk(args[2])
        except ValueError as e:
            await update.message.reply_text(
                f"<b>INVALID RISK</b>  {e}\n\n  Use <code>3%</code> or <code>150$</code>.",
                parse_mode="HTML",
            )
            return

        try:
            new_sl = float(args[3])
            if new_sl <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("<b>INVALID SL</b>  Enter a positive number.", parse_mode="HTML")
            return

        if trade.side == Side.LONG and new_sl >= dca_price:
            await update.message.reply_text(
                f"<b>INVALID SL</b>  ${_fmt(new_sl)} must be <b>below</b> DCA price ${_fmt(dca_price)} for LONG.",
                parse_mode="HTML",
            )
            return
        if trade.side == Side.SHORT and new_sl <= dca_price:
            await update.message.reply_text(
                f"<b>INVALID SL</b>  ${_fmt(new_sl)} must be <b>above</b> DCA price ${_fmt(dca_price)} for SHORT.",
                parse_mode="HTML",
            )
            return

        msg = await update.message.reply_text("⏳ Calculating DCA sizing...")
        try:
            if risk_type_str == "percent":
                equity = await self._client.get_total_balance()
                s = get_settings()
                base = s.risk_balance if s.risk_balance > 0 else equity
                risk_amount = base * risk_value / 100.0
            else:
                risk_amount = risk_value

            stop_distance = abs(dca_price - new_sl)
            if stop_distance == 0:
                await msg.edit_text("<b>INVALID</b>  DCA price and SL price cannot be the same.", parse_mode="HTML")
                return

            sym_info  = await self._client.get_symbol_info(pair)
            qp        = sym_info["basePrecision"]
            min_qty   = sym_info["minTradeVolume"]
            raw_qty   = risk_amount / stop_distance
            dca_qty   = round(raw_qty, qp)

            if dca_qty < min_qty:
                await msg.edit_text(
                    f"<b>QTY TOO SMALL</b>  Calculated {dca_qty} tokens is below exchange minimum {min_qty}.\n\n"
                    f"  Increase risk amount or widen the stop distance.",
                    parse_mode="HTML",
                )
                return

            dca_notional = dca_qty * dca_price
            side_tag     = "🟢 LONG" if trade.side == Side.LONG else "🔴 SHORT"

            # Strategy multiplier qty (neil=2×, saltwayer=2.5×)
            strategy   = trade.strategy or ""
            mult       = 2.0 if strategy == "neil" else (2.5 if strategy == "saltwayer" else None)
            mult_label = f"{mult}×" if mult else None

            # Option A: lock qty to entry×mult, derive risk
            mult_qty  = round(trade.position_size * mult, qp) if mult else None
            mult_risk = round(mult_qty * stop_distance, 2) if mult_qty else None

            # Option B: combined formula — back-calculate corrected entry qty
            # so that entry_risk + dca_risk = target_risk exactly, with dca=M×entry
            # entry_dist = |entry - new_sl|, dca_dist = |dca_price - new_sl|
            # dca_qty_corrected = M × target_risk / (entry_dist + M × dca_dist)
            corr_qty      = None
            corr_risk     = None
            corr_entry_qty = None
            if mult and trade.entry:
                is_long = trade.side == Side.LONG
                entry_dist = (trade.entry - new_sl) if is_long else (new_sl - trade.entry)
                dca_dist   = (dca_price - new_sl)   if is_long else (new_sl - dca_price)
                denom = entry_dist + mult * dca_dist
                if denom > 0:
                    corr_entry_qty = risk_amount / denom
                    corr_qty       = round(corr_entry_qty * mult, qp)
                    corr_risk      = round(corr_qty * dca_dist, 2)
                    corr_total     = round(corr_entry_qty * entry_dist + corr_qty * dca_dist, 2)

            # Build keyboard
            kb_rows = []
            if mult_qty and mult_qty >= min_qty:
                kb_rows.append([InlineKeyboardButton(
                    f"📏  {mult_label}× entry size ({mult_qty} tokens)",
                    callback_data=f"adddca:confirm:{pair}:{dca_price}:{mult_qty}:{new_sl}:{mult_risk:.2f}"
                )])
            if corr_qty and corr_qty >= min_qty:
                kb_rows.append([InlineKeyboardButton(
                    f"🎯  Exact {risk_amount:,.2f}$ total ({corr_qty} tokens)",
                    callback_data=f"adddca:confirm:{pair}:{dca_price}:{corr_qty}:{new_sl}:{corr_risk:.2f}"
                )])
            kb_rows.append([InlineKeyboardButton(
                f"📐  Risk-sized ({dca_qty} tokens)",
                callback_data=f"adddca:confirm:{pair}:{dca_price}:{dca_qty}:{new_sl}:{risk_amount:.2f}"
            )])
            kb_rows.append([InlineKeyboardButton("✖  Cancel", callback_data="adddca:cancel")])
            kb = InlineKeyboardMarkup(kb_rows)

            strategy_line = ""
            if mult and trade.entry:
                if mult_qty and mult_qty >= min_qty:
                    strategy_line += (
                        f"  <code>{'Mult sized':<11}</code>  {mult_label} entry = {mult_qty} tokens  →  risk ${mult_risk:,.2f}\n"
                    )
                if corr_qty and corr_qty >= min_qty:
                    strategy_line += (
                        f"  <code>{'Exact 5%':<11}</code>  corrected = {corr_qty} tokens  →  DCA risk ${corr_risk:,.2f}  (total ${corr_total:,.2f})\n"
                    )

            await msg.edit_text(
                f"<b>DCA SUMMARY — {pair}</b>\n"
                f"<code>{sep}</code>\n\n"
                f"  {side_tag}\n\n"
                f"  <code>Entry      </code>  {trade.position_size} tokens @ ${_fmt(trade.entry)}\n"
                f"  <code>DCA Price  </code>  ${_fmt(dca_price)}\n"
                f"  <code>New SL     </code>  ${_fmt(new_sl)}\n"
                f"  <code>Old SL     </code>  ${_fmt(trade.sl)}\n"
                f"  <code>Risk input </code>  ${risk_amount:,.2f}\n"
                f"<code>{'─'*28}</code>\n"
                f"{strategy_line}"
                f"  <code>{'Risk-sized':<11}</code>  {dca_qty} tokens  →  risk ${risk_amount:,.2f}\n\n"
                f"  <i>🎯 Exact uses combined formula so entry+DCA = ${risk_amount:,.2f} exactly at new SL.</i>",
                parse_mode="HTML",
                reply_markup=kb,
            )

        except APIError as e:
            await msg.edit_text(M.api_error("DCA sizing", e), parse_mode="HTML")
        except Exception as e:
            logger.error(f"cmd_dca sizing error: {e}", exc_info=True)
            await msg.edit_text(f"<b>ERROR</b>  <code>{e}</code>", parse_mode="HTML")

    @auth_required
    async def _handle_adddca(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        data = query.data

        if data == "adddca:cancel":
            await query.edit_message_text("<b>DCA CANCELLED</b>", parse_mode="HTML")
            return

        try:
            parts     = data.split(":")
            pair      = parts[2]
            dca_price = float(parts[3])
            dca_qty   = float(parts[4])
            new_sl    = float(parts[5])
            risk_amt  = float(parts[6])
        except Exception:
            await query.edit_message_text("<b>ERROR</b>  Malformed callback data.", parse_mode="HTML")
            return

        sep = "─" * 28
        await query.edit_message_text(f"⏳ Placing DCA for {pair}...")
        try:
            order_id = await self._om.add_dca_order(pair, dca_price, dca_qty, new_sl)
            await query.edit_message_text(
                f"<b>DCA PLACED</b>  ✅\n"
                f"<code>{sep}</code>\n\n"
                f"  <b>{pair}</b>\n"
                f"  <code>DCA Price  </code>  ${_fmt(dca_price)}\n"
                f"  <code>Qty        </code>  {dca_qty} tokens\n"
                f"  <code>Risk       </code>  ${risk_amt:,.2f}\n"
                f"  <code>New SL     </code>  ${_fmt(new_sl)}\n"
                f"  <code>Order      </code>  <code>{order_id}</code>\n\n"
                f"  <i>DCA order live. SL moved to ${_fmt(new_sl)}.</i>",
                parse_mode="HTML",
            )
        except (ValueError, APIError) as e:
            await query.edit_message_text(
                f"<b>DCA FAILED</b>\n\n<code>{e}</code>",
                parse_mode="HTML",
            )

    async def cmd_unknown(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

        known = {
            "/start", "/help", "/commands", "/balance", "/positions",
            "/closeall", "/cancelpair", "/trade", "/history", "/stats",
            "/cancel", "/debug", "/settings", "/sync", "/resync", "/modifysl", "/movesl", "/setsl", "/setleverage", "/setstrategy", "/tp", "/dca",
        }
        text = update.message.text or ""
        cmd  = text.split()[0].lower().split("@")[0]
        if cmd in known:
            return
        await update.message.reply_text(
            f"<b>UNKNOWN COMMAND</b>  <code>{cmd}</code>\n\n"
            f"  Use /commands to see all available commands.",
            parse_mode="HTML",
        )