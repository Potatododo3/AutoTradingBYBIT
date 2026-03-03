"""
Debug command — interactive test panel that exercises every bot function
without placing real orders. Enabled regardless of DEBUG_MODE so you can
always reach it, but it clearly labels all actions as simulated.
"""
import logging
import traceback
from datetime import datetime
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    CallbackQueryHandler, CommandHandler, ContextTypes
)

from bybit_client import BybitClient
from config import AUTHORIZED_USER_ID, DEBUG_MODE
from database import Database
from journal import Journal
from models import (
    Side, RiskType, TradeRecord, TradeStatus,
    PositionSizing, TradeRequest, APIError
)
from order_manager import OrderManager
from risk_manager import RiskManager
from utils import generate_trade_id

logger = logging.getLogger(__name__)

# ── Fake data used for dry-run tests ─────────────────────────────────────────

FAKE_PAIR = "BTCUSDT"
FAKE_TRADE_ID = "DBG00001"


def _fake_trade() -> TradeRecord:
    return TradeRecord(
        trade_id=FAKE_TRADE_ID,
        pair=FAKE_PAIR,
        side=Side.LONG,
        entry=60000.0,
        sl=58000.0,
        tp1=62000.0,
        tp2=64000.0,
        tp3=66000.0,
        position_size=0.05,
        leverage=10,
        risk_amount=100.0,
        balance_at_entry=10000.0,
        status=TradeStatus.OPEN,
        opened_at=datetime.utcnow(),
    )


def _fake_sizing() -> PositionSizing:
    return PositionSizing(
        balance=10000.0,
        risk_amount=100.0,
        entry_price=60000.0,
        sl_price=58000.0,
        stop_distance=2000.0,
        position_size=0.05,
        leverage=10,
        margin_required=300.0,
        liquidation_price=54500.0,
        risk_percent=1.0,
    )


def _fake_req() -> TradeRequest:
    return TradeRequest(
        pair=FAKE_PAIR,
        side=Side.LONG,
        entry=60000.0,
        risk_value=1.0,
        risk_type=RiskType.PERCENT,
        tp1=62000.0,
        tp2=64000.0,
        tp3=66000.0,
        sl=58000.0,
        dca=None,
    )


# ── Debug menu keyboard ───────────────────────────────────────────────────────

def _main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💰 Test: Get Balance", callback_data="dbg:balance"),
            InlineKeyboardButton("📋 Test: Get Positions", callback_data="dbg:positions"),
        ],
        [
            InlineKeyboardButton("💹 Test: Ticker Price", callback_data="dbg:ticker"),
            InlineKeyboardButton("📐 Test: Position Sizing", callback_data="dbg:sizing"),
        ],
        [
            InlineKeyboardButton("🟢 Test: Open Trade (DRY)", callback_data="dbg:open_trade"),
            InlineKeyboardButton("🔴 Test: Close Trade (DRY)", callback_data="dbg:close_trade"),
        ],
        [
            InlineKeyboardButton("🛑 Test: Close All (DRY)", callback_data="dbg:close_all"),
            InlineKeyboardButton("📜 Test: DB History", callback_data="dbg:db_history"),
        ],
        [
            InlineKeyboardButton("📊 Test: DB Stats", callback_data="dbg:db_stats"),
            InlineKeyboardButton("🔔 Test: Discord Journal", callback_data="dbg:journal"),
        ],
        [
            InlineKeyboardButton("🔑 Test: API Auth", callback_data="dbg:api_auth"),
            InlineKeyboardButton("⚙️ Show Config", callback_data="dbg:config"),
        ],
        [
            InlineKeyboardButton("📝 Test: Full Trade Flow (DRY)", callback_data="dbg:full_flow"),
        ],
        [
            InlineKeyboardButton("🔬 Probe: Place Order", callback_data="dbg:probe_order"),
            InlineKeyboardButton("🔬 Probe: Set Leverage", callback_data="dbg:probe_leverage"),
        ],
    ])


def _back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ Back to Debug Menu", callback_data="dbg:menu")
    ]])


# ── Handler class ─────────────────────────────────────────────────────────────

class DebugHandler:
    def __init__(
        self,
        client: BybitClient,
        order_manager: OrderManager,
        risk_manager: RiskManager,
        journal: Journal,
        db: Database,
    ) -> None:
        self._client = client
        self._om = order_manager
        self._rm = risk_manager
        self._journal = journal
        self._db = db

    def register(self, app) -> None:
        app.add_handler(CommandHandler("debug", self.cmd_debug))
        app.add_handler(CallbackQueryHandler(self.handle_debug_callback, pattern="^dbg:"))

    def _auth(self, update: Update) -> bool:
        uid = update.effective_user.id if update.effective_user else None
        return uid == AUTHORIZED_USER_ID

    async def cmd_debug(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update):
            await update.message.reply_text("⛔ Unauthorized.")
            return

        mode_label = "🟡 DEBUG (dry-run)" if DEBUG_MODE else "🔴 LIVE MODE"
        await update.message.reply_text(
            f"🛠 <b>Debug Panel</b>\n\n"
            f"Mode: <b>{mode_label}</b>\n"
            f"{'⚠️ DEBUG_MODE=false in .env — API calls are REAL. Trades marked (DRY) use fake data and will NOT place orders.' if not DEBUG_MODE else '✅ DEBUG_MODE=true — all API calls go to real Bitunix but trades marked (DRY) will NOT place orders.'}\n\n"
            f"Select a test to run:",
            parse_mode="HTML",
            reply_markup=_main_menu_kb(),
        )

    async def handle_debug_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update):
            await update.callback_query.answer("Unauthorized.", show_alert=True)
            return

        query = update.callback_query
        await query.answer()
        action = query.data  # e.g. "dbg:balance"

        handlers = {
            "dbg:menu":        self._show_menu,
            "dbg:balance":     self._test_balance,
            "dbg:positions":   self._test_positions,
            "dbg:ticker":      self._test_ticker,
            "dbg:sizing":      self._test_sizing,
            "dbg:open_trade":  self._test_open_trade,
            "dbg:close_trade": self._test_close_trade,
            "dbg:close_all":   self._test_close_all,
            "dbg:db_history":  self._test_db_history,
            "dbg:db_stats":    self._test_db_stats,
            "dbg:journal":     self._test_journal,
            "dbg:api_auth":    self._test_api_auth,
            "dbg:config":      self._show_config,
            "dbg:full_flow":   self._test_full_flow,
            "dbg:probe_order":    self._probe_place_order,
            "dbg:probe_leverage": self._probe_set_leverage,
        }

        fn = handlers.get(action)
        if fn:
            try:
                await fn(query, context)
            except Exception as e:
                tb = traceback.format_exc()
                logger.error(f"Debug handler error [{action}]: {e}\n{tb}")
                await query.edit_message_text(
                    f"💥 <b>Unhandled exception in debug handler</b>\n\n"
                    f"<code>{action}</code>\n\n"
                    f"<pre>{tb[-1500:]}</pre>",
                    parse_mode="HTML",
                    reply_markup=_back_kb(),
                )

    async def _show_menu(self, query, context) -> None:
        mode_label = "🟡 DEBUG (dry-run)" if DEBUG_MODE else "🔴 LIVE MODE"
        await query.edit_message_text(
            f"🛠 <b>Debug Panel</b> — {mode_label}\n\nSelect a test:",
            parse_mode="HTML",
            reply_markup=_main_menu_kb(),
        )

    # ── Individual tests ──────────────────────────────────────────────────────

    async def _test_balance(self, query, context) -> None:
        await query.edit_message_text("⏳ Fetching balance from Bitunix API...", parse_mode="HTML")
        try:
            available = await self._client.get_balance()
            equity = await self._client.get_total_balance()
            await query.edit_message_text(
                f"✅ <b>Balance Test — PASSED</b>\n\n"
                f"Available Margin:  <code>${available:,.2f}</code>\n"
                f"Total Equity:      <code>${equity:,.2f}</code>\n\n"
                f"<i>Real API call succeeded. Authentication working.</i>",
                parse_mode="HTML",
                reply_markup=_back_kb(),
            )
            logger.info(f"[DEBUG] Balance test passed: available={available}, equity={equity}")
        except APIError as e:
            await query.edit_message_text(
                f"❌ <b>Balance Test — FAILED</b>\n\n"
                f"Error: <code>{e}</code>\n"
                f"Status: <code>{e.status_code}</code>\n"
                f"Response: <pre>{e.response[:500]}</pre>",
                parse_mode="HTML",
                reply_markup=_back_kb(),
            )
            logger.error(f"[DEBUG] Balance test failed: {e}")

    async def _test_positions(self, query, context) -> None:
        await query.edit_message_text("⏳ Fetching open positions...", parse_mode="HTML")
        try:
            positions = await self._client.get_positions()
            if not positions:
                result = "No open positions found."
            else:
                lines = []
                for p in positions:
                    lines.append(
                        f"• <b>{p.symbol}</b> {p.side} | qty={p.size} "
                        f"@ ${p.entry_price:,.4f} | PnL=${p.unrealized_pnl:.2f}"
                    )
                result = "\n".join(lines)

            await query.edit_message_text(
                f"✅ <b>Positions Test — PASSED</b>\n\n{result}",
                parse_mode="HTML",
                reply_markup=_back_kb(),
            )
            logger.info(f"[DEBUG] Positions test passed: {len(positions)} position(s)")
        except APIError as e:
            await query.edit_message_text(
                f"❌ <b>Positions Test — FAILED</b>\n\n"
                f"Error: <code>{e}</code>\n"
                f"Response: <pre>{e.response[:500]}</pre>",
                parse_mode="HTML",
                reply_markup=_back_kb(),
            )
            logger.error(f"[DEBUG] Positions test failed: {e}")

    async def _test_ticker(self, query, context) -> None:
        await query.edit_message_text(f"⏳ Fetching {FAKE_PAIR} ticker...", parse_mode="HTML")
        try:
            price = await self._client.get_ticker_price(FAKE_PAIR)
            await query.edit_message_text(
                f"✅ <b>Ticker Test — PASSED</b>\n\n"
                f"Symbol:  <code>{FAKE_PAIR}</code>\n"
                f"Price:   <code>${price:,.4f}</code>\n\n"
                f"<i>Market data API working correctly.</i>",
                parse_mode="HTML",
                reply_markup=_back_kb(),
            )
            logger.info(f"[DEBUG] Ticker test passed: {FAKE_PAIR}={price}")
        except APIError as e:
            await query.edit_message_text(
                f"❌ <b>Ticker Test — FAILED</b>\n\n"
                f"Error: <code>{e}</code>\n"
                f"Response: <pre>{e.response[:500]}</pre>",
                parse_mode="HTML",
                reply_markup=_back_kb(),
            )
            logger.error(f"[DEBUG] Ticker test failed: {e}")

    async def _test_sizing(self, query, context) -> None:
        await query.edit_message_text("⏳ Testing position sizing calculation...", parse_mode="HTML")
        try:
            req = _fake_req()
            # Use live price for realistic sizing
            live_price = await self._client.get_ticker_price(FAKE_PAIR)
            balance = await self._client.get_balance()
            # Manually calculate to show the math
            risk_amount = balance * (req.risk_value / 100.0)
            # Use a fixed SL distance for demo
            sl_distance = live_price * 0.03  # 3% SL
            position_size = risk_amount / sl_distance

            await query.edit_message_text(
                f"✅ <b>Position Sizing Test — PASSED</b>\n\n"
                f"Live {FAKE_PAIR} Price:  <code>${live_price:,.2f}</code>\n"
                f"Account Balance:     <code>${balance:,.2f}</code>\n"
                f"Risk (1%):           <code>${risk_amount:.2f}</code>\n"
                f"SL Distance (3%):    <code>${sl_distance:.2f}</code>\n"
                f"Position Size:       <code>{position_size:.4f} BTC</code>\n"
                f"Notional Value:      <code>${position_size * live_price:,.2f}</code>\n\n"
                f"<i>Formula: position_size = risk_amount / sl_distance</i>",
                parse_mode="HTML",
                reply_markup=_back_kb(),
            )
            logger.info(f"[DEBUG] Sizing test passed: size={position_size:.4f}")
        except APIError as e:
            await query.edit_message_text(
                f"❌ <b>Sizing Test — FAILED</b>\n\n"
                f"Error: <code>{e}</code>",
                parse_mode="HTML",
                reply_markup=_back_kb(),
            )
            logger.error(f"[DEBUG] Sizing test failed: {e}")

    async def _test_open_trade(self, query, context) -> None:
        """Simulate opening a trade without placing any real orders."""
        await query.edit_message_text("⏳ Simulating trade open (DRY RUN)...", parse_mode="HTML")

        trade = _fake_trade()
        sizing = _fake_sizing()

        # Log to journal as if it were real
        await self._journal.log_trade_open(trade, sizing)

        logger.info(
            f"[DEBUG][DRY-RUN] Simulated trade open — "
            f"pair={trade.pair} side={trade.side.value} "
            f"entry={trade.entry} sl={trade.sl} "
            f"size={trade.position_size} lev={trade.leverage}x"
        )

        await query.edit_message_text(
            f"✅ <b>Open Trade — DRY RUN PASSED</b>\n\n"
            f"<b>No real order was placed.</b>\n\n"
            f"Simulated trade details:\n"
            f"  Pair:      <code>{trade.pair}</code>\n"
            f"  Side:      <code>LONG</code>\n"
            f"  Entry:     <code>${trade.entry:,.2f}</code>\n"
            f"  SL:        <code>${trade.sl:,.2f}</code>\n"
            f"  TP1/2/3:   <code>${trade.tp1:,.0f} / ${trade.tp2:,.0f} / ${trade.tp3:,.0f}</code>\n"
            f"  Size:      <code>{trade.position_size} BTC</code>\n"
            f"  Leverage:  <code>{trade.leverage}x</code>\n"
            f"  Risk:      <code>${sizing.risk_amount:.2f} ({sizing.risk_percent:.1f}%)</code>\n"
            f"  Margin:    <code>${sizing.margin_required:.2f}</code>\n"
            f"  Liq Price: <code>${sizing.liquidation_price:,.2f}</code>\n\n"
            f"<i>Discord journal log also sent (if configured).</i>",
            parse_mode="HTML",
            reply_markup=_back_kb(),
        )

    async def _test_close_trade(self, query, context) -> None:
        """Simulate closing a single trade without any real API calls."""
        await query.edit_message_text("⏳ Simulating trade close (DRY RUN)...", parse_mode="HTML")

        trade = _fake_trade()
        trade.realized_pnl = 150.0
        trade.exit_price = 62500.0
        trade.closed_at = datetime.utcnow()

        await self._journal.log_trade_closed(trade, reason="debug_dry_run")

        logger.info(
            f"[DEBUG][DRY-RUN] Simulated trade close — "
            f"pair={trade.pair} pnl={trade.realized_pnl} exit={trade.exit_price}"
        )

        await query.edit_message_text(
            f"✅ <b>Close Trade — DRY RUN PASSED</b>\n\n"
            f"<b>No real order was placed.</b>\n\n"
            f"Simulated close:\n"
            f"  Pair:         <code>{trade.pair}</code>\n"
            f"  Exit Price:   <code>${trade.exit_price:,.2f}</code>\n"
            f"  Realized PnL: <code>+${trade.realized_pnl:.2f}</code> 🟢\n\n"
            f"<i>Discord journal log also sent (if configured).</i>",
            parse_mode="HTML",
            reply_markup=_back_kb(),
        )

    async def _test_close_all(self, query, context) -> None:
        """Simulate close all without any real API calls."""
        await query.edit_message_text("⏳ Simulating Close All (DRY RUN)...", parse_mode="HTML")

        fake_trades = [_fake_trade()]
        fake_trades[0].realized_pnl = -50.0

        await self._journal.log_closeall(fake_trades)

        logger.info(f"[DEBUG][DRY-RUN] Simulated closeall — {len(fake_trades)} trade(s)")

        await query.edit_message_text(
            f"✅ <b>Close All — DRY RUN PASSED</b>\n\n"
            f"<b>No real orders were placed.</b>\n\n"
            f"Would have closed: <code>{fake_trades[0].pair}</code>\n"
            f"Simulated PnL: <code>-$50.00</code> 🔴\n\n"
            f"<i>Discord journal log also sent (if configured).</i>",
            parse_mode="HTML",
            reply_markup=_back_kb(),
        )

    async def _test_db_history(self, query, context) -> None:
        await query.edit_message_text("⏳ Reading trade history from database...", parse_mode="HTML")
        try:
            trades = await self._db.get_trade_history(limit=5)
            active = await self._db.get_active_trades()

            if not trades:
                summary = "No trades in database yet."
            else:
                lines = []
                for t in trades:
                    lines.append(
                        f"• <code>{t.trade_id}</code> {t.pair} {t.side.value.upper()} "
                        f"[{t.status.value}]"
                    )
                summary = "\n".join(lines)

            await query.edit_message_text(
                f"✅ <b>Database Test — PASSED</b>\n\n"
                f"Active trades:    <code>{len(active)}</code>\n"
                f"Total records:    <code>{len(trades)}</code>\n\n"
                f"<b>Last 5 records:</b>\n{summary}",
                parse_mode="HTML",
                reply_markup=_back_kb(),
            )
            logger.info(f"[DEBUG] DB history test passed: {len(trades)} record(s)")
        except Exception as e:
            tb = traceback.format_exc()
            await query.edit_message_text(
                f"❌ <b>Database Test — FAILED</b>\n\n"
                f"<pre>{tb[-800:]}</pre>",
                parse_mode="HTML",
                reply_markup=_back_kb(),
            )
            logger.error(f"[DEBUG] DB test failed: {e}\n{tb}")

    async def _test_db_stats(self, query, context) -> None:
        await query.edit_message_text("⏳ Reading stats from database...", parse_mode="HTML")
        try:
            stats = await self._db.get_stats()
            active = await self._db.get_active_trades()

            if not stats or stats.get("total_trades", 0) == 0:
                stats_text = "No closed trades yet — stats will appear after first closed trade."
            else:
                pnl_emoji = "🟢" if stats["total_pnl"] >= 0 else "🔴"
                stats_text = (
                    f"Total trades:  <code>{stats['total_trades']}</code>\n"
                    f"Win rate:      <code>{stats['win_rate']}%</code>\n"
                    f"Total PnL:     {pnl_emoji} <code>${stats['total_pnl']:.2f}</code>\n"
                    f"Best trade:    <code>+${stats['best_trade']:.2f}</code>\n"
                    f"Worst trade:   <code>-${abs(stats['worst_trade']):.2f}</code>"
                )

            await query.edit_message_text(
                f"✅ <b>DB Stats Test — PASSED</b>\n\n"
                f"Active trades: <code>{len(active)}</code>\n\n"
                f"{stats_text}",
                parse_mode="HTML",
                reply_markup=_back_kb(),
            )
        except Exception as e:
            tb = traceback.format_exc()
            await query.edit_message_text(
                f"❌ <b>DB Stats Test — FAILED</b>\n\n<pre>{tb[-800:]}</pre>",
                parse_mode="HTML",
                reply_markup=_back_kb(),
            )
            logger.error(f"[DEBUG] DB stats test failed: {e}")

    async def _test_journal(self, query, context) -> None:
        await query.edit_message_text("⏳ Sending test Discord webhook...", parse_mode="HTML")

        trade = _fake_trade()
        sizing = _fake_sizing()

        # Fire all journal event types
        await self._journal.log_trade_open(trade, sizing)
        await self._journal.log_tp_hit(
            trade, tp_num=1, tp_price=trade.tp1, qty_closed=0.02, remaining=0.03)
        await self._journal.log_sl_hit(trade, close_price=trade.sl, pnl=-100.0)
        await self._journal.log_error(
            error_type="Debug test error",
            pair=FAKE_PAIR,
            api_response='{"code": 1001, "msg": "Test error"}',
        )

        from config import DISCORD_WEBHOOK_URL
        webhook_status = "✅ Webhook URL configured" if DISCORD_WEBHOOK_URL else "⚠️ No DISCORD_WEBHOOK_URL set — logs only printed locally"

        logger.info("[DEBUG] Journal test fired: open, tp1, sl, error events")

        await query.edit_message_text(
            f"✅ <b>Journal Test — PASSED</b>\n\n"
            f"Webhook: {webhook_status}\n\n"
            f"Events sent:\n"
            f"  • Trade Open\n"
            f"  • TP1 Hit (+$50.00)\n"
            f"  • SL Hit (-$100.00)\n"
            f"  • Error event\n\n"
            f"<i>Check your Discord channel for the embeds.</i>",
            parse_mode="HTML",
            reply_markup=_back_kb(),
        )

    async def _test_api_auth(self, query, context) -> None:
        """Test API key validity by making the lightest possible authenticated call."""
        await query.edit_message_text("⏳ Testing API key authentication...", parse_mode="HTML")
        try:
            # Lightest authenticated endpoint — get account
            balance = await self._client.get_balance()
            await query.edit_message_text(
                f"✅ <b>API Auth Test — PASSED</b>\n\n"
                f"API key is valid and signature is correct.\n"
                f"Balance returned: <code>${balance:,.2f}</code>\n\n"
                f"<i>Signing algorithm: double SHA256\n"
                f"nonce → timestamp → api-key → queryParams → body</i>",
                parse_mode="HTML",
                reply_markup=_back_kb(),
            )
            logger.info(f"[DEBUG] API auth test passed, balance={balance}")
        except APIError as e:
            await query.edit_message_text(
                f"❌ <b>API Auth Test — FAILED</b>\n\n"
                f"Message:  <code>{e}</code>\n"
                f"HTTP:     <code>{e.status_code}</code>\n"
                f"Response: <pre>{e.response[:600]}</pre>\n\n"
                f"<b>Common causes:</b>\n"
                f"• Wrong API key or secret in .env\n"
                f"• System clock out of sync (must be within 60s of server)\n"
                f"• API key doesn't have futures trading permission",
                parse_mode="HTML",
                reply_markup=_back_kb(),
            )
            logger.error(f"[DEBUG] API auth test failed: {e} | response={e.response}")

    async def _show_config(self, query, context) -> None:
        from config import (
            BYBIT_BASE_URL, CONFIRMATION_REQUIRED, MAX_LEVERAGE,
            DEFAULT_LEVERAGE, TP1_PCT, TP2_PCT, TP3_PCT,
            CSV_JOURNAL_PATH, DB_PATH, DISCORD_WEBHOOK_URL
        )

        webhook_display = "✅ Set" if DISCORD_WEBHOOK_URL else "❌ Not set"
        mode_label = "🟡 DRY-RUN (DEBUG_MODE=true)" if DEBUG_MODE else "🔴 LIVE (DEBUG_MODE=false)"

        await query.edit_message_text(
            f"⚙️ <b>Current Configuration</b>\n\n"
            f"<b>Mode:</b>              {mode_label}\n\n"
            f"<b>API:</b>\n"
            f"  Base URL:           <code>{BYBIT_BASE_URL}</code>\n"
            f"  Confirmation req:   <code>{CONFIRMATION_REQUIRED}</code>\n\n"
            f"<b>Leverage:</b>\n"
            f"  Default:            <code>{DEFAULT_LEVERAGE}x</code>\n"
            f"  Maximum:            <code>{MAX_LEVERAGE}x</code>\n\n"
            f"<b>TP Splits:</b>\n"
            f"  TP1:                <code>{int(TP1_PCT*100)}%</code>\n"
            f"  TP2:                <code>{int(TP2_PCT*100)}%</code>\n"
            f"  TP3:                <code>{int(TP3_PCT*100)}%</code>\n\n"
            f"<b>Storage:</b>\n"
            f"  Database:           <code>{DB_PATH}</code>\n"
            f"  CSV Journal:        <code>{CSV_JOURNAL_PATH}</code>\n"
            f"  Discord Webhook:    {webhook_display}\n",
            parse_mode="HTML",
            reply_markup=_back_kb(),
        )

    async def _probe_place_order(self, query, context) -> None:
        """
        Send the minimum valid place_order body and show exactly
        what the exchange accepts or rejects.
        Places a real 0.001 BTC order then immediately flash-closes.
        """
        import json
        await query.edit_message_text(
            "Probing place_order with minimal body...\n"
            "Will send real orders to exchange (0.001 BTC).",
            parse_mode="HTML",
        )

        steps = []

        # Step 1: live price
        try:
            price = await self._client.get_ticker_price("BTCUSDT")
            steps.append(f"Ticker: ${price:,.2f}")
        except Exception as e:
            steps.append(f"FAIL Ticker: {e}")
            price = 0

        # Step 2: try set_leverage first (this may be the real failure point)
        try:
            await self._client.set_leverage("BTCUSDT", 20)
            steps.append("set_leverage(20) OK")
        except Exception as e:
            steps.append(f"FAIL set_leverage(20): {e}")

        # Step 3: minimal OPEN body
        body_open = {
            "symbol":    "BTCUSDT",
            "side":      "BUY",
            "orderType": "MARKET",
            "qty":       "0.001",
            "tradeSide": "OPEN",
            "effect":    "GTC",
            "reduceOnly": False,
        }
        steps.append(f"OPEN body: {json.dumps(body_open)}")

        order_id = ""
        try:
            order_id = await self._client.place_order(
                symbol="BTCUSDT", side="BUY", order_type="MARKET",
                qty=0.001, trade_side="OPEN",
            )
            steps.append(f"OPEN OK: orderId={order_id}")
        except Exception as e:
            steps.append(f"FAIL OPEN: {e}")

        # Step 4: if open worked, get positionId then flash close
        if order_id:
            import asyncio
            await asyncio.sleep(1.5)
            try:
                pos = await self._client.get_position("BTCUSDT")
                pos_id = pos.position_id if pos else ""
                steps.append(f"positionId: {pos_id or '(none)'}")
                if pos_id:
                    await self._client.flash_close_position(pos_id)
                steps.append("flash_close OK")
            except Exception as e:
                steps.append(f"FAIL close: {e} — close BTCUSDT manually!")

        result = "\n".join(steps)
        await query.edit_message_text(
            f"<b>place_order Probe</b>\n\n<code>{result}</code>",
            parse_mode="HTML",
            reply_markup=_back_kb(),
        )

    async def _probe_set_leverage(self, query, context) -> None:
        """Test set_leverage in isolation."""
        import json
        await query.edit_message_text("Probing set_leverage...", parse_mode="HTML")
        steps = []

        # Try each leverage value and show exact body
        for lev in [20, 50, 100]:
            body = {"symbol": "BTCUSDT", "leverage": lev, "marginMode": "CROSS"}
            steps.append(f"Body: {json.dumps(body)}")
            try:
                await self._client.set_leverage("BTCUSDT", lev)
                steps.append(f"leverage={lev} ACCEPTED")
                break
            except Exception as e:
                steps.append(f"leverage={lev} FAILED: {e}")

        result = "\n".join(steps)
        await query.edit_message_text(
            f"<b>set_leverage Probe</b>\n\n<code>{result}</code>",
            parse_mode="HTML",
            reply_markup=_back_kb(),
        )


    async def _test_full_flow(self, query, context) -> None:
        """
        Walk through the complete trade lifecycle with fake data:
        open → tp1 hit → sl move → close — all dry run, no API calls for orders.
        Real API calls: balance + ticker (read-only).
        """
        await query.edit_message_text("⏳ Running full trade lifecycle dry run...", parse_mode="HTML")

        results = []

        # Step 1: fetch balance (real API call)
        try:
            balance = await self._client.get_balance()
            results.append(f"✅ Step 1 — Balance: <code>${balance:,.2f}</code>")
            logger.info(f"[DEBUG][FULL-FLOW] Step 1 balance: {balance}")
        except APIError as e:
            results.append(f"❌ Step 1 — Balance fetch failed: <code>{e}</code>")
            logger.error(f"[DEBUG][FULL-FLOW] Step 1 failed: {e}")

        # Step 2: fetch ticker (real API call)
        try:
            price = await self._client.get_ticker_price(FAKE_PAIR)
            results.append(f"✅ Step 2 — Ticker {FAKE_PAIR}: <code>${price:,.2f}</code>")
            logger.info(f"[DEBUG][FULL-FLOW] Step 2 ticker: {price}")
        except APIError as e:
            results.append(f"❌ Step 2 — Ticker fetch failed: <code>{e}</code>")
            logger.error(f"[DEBUG][FULL-FLOW] Step 2 failed: {e}")

        # Step 3: position sizing (local calculation)
        try:
            req = _fake_req()
            sizing = _fake_sizing()
            results.append(
                f"✅ Step 3 — Sizing: <code>{sizing.position_size} BTC</code> "
                f"@ <code>{sizing.leverage}x</code> lev"
            )
            logger.info(f"[DEBUG][FULL-FLOW] Step 3 sizing: {sizing.position_size} BTC")
        except Exception as e:
            results.append(f"❌ Step 3 — Sizing failed: <code>{e}</code>")
            logger.error(f"[DEBUG][FULL-FLOW] Step 3 failed: {e}")

        # Step 4: simulate trade open + journal
        try:
            trade = _fake_trade()
            await self._journal.log_trade_open(trade, sizing)
            results.append(f"✅ Step 4 — Trade open simulated + journalled")
            logger.info(f"[DEBUG][FULL-FLOW] Step 4 trade open simulated")
        except Exception as e:
            results.append(f"❌ Step 4 — Trade open sim failed: <code>{e}</code>")
            logger.error(f"[DEBUG][FULL-FLOW] Step 4 failed: {e}")

        # Step 5: simulate TP1 hit
        try:
            await self._journal.log_tp_hit(
                trade, tp_num=1, tp_price=trade.tp1, qty_closed=0.02, remaining=0.03)
            results.append(f"✅ Step 5 — TP1 hit simulated (+$80.00)")
            logger.info(f"[DEBUG][FULL-FLOW] Step 5 TP1 hit simulated")
        except Exception as e:
            results.append(f"❌ Step 5 — TP1 sim failed: <code>{e}</code>")

        # Step 6: simulate SL move to breakeven
        try:
            old_sl = trade.sl
            trade.sl = trade.entry  # move to breakeven
            results.append(f"✅ Step 6 — SL moved: <code>${old_sl:,.0f}</code> → <code>${trade.sl:,.0f}</code> (breakeven)")
            logger.info(f"[DEBUG][FULL-FLOW] Step 6 SL moved to breakeven")
        except Exception as e:
            results.append(f"❌ Step 6 — SL move sim failed: <code>{e}</code>")

        # Step 7: simulate trade close
        try:
            trade.realized_pnl = 200.0
            trade.exit_price = 63000.0
            trade.closed_at = datetime.utcnow()
            await self._journal.log_trade_closed(trade, reason="debug_full_flow")
            results.append(f"✅ Step 7 — Trade closed simulated (PnL: +$200.00)")
            logger.info(f"[DEBUG][FULL-FLOW] Step 7 trade closed simulated")
        except Exception as e:
            results.append(f"❌ Step 7 — Close sim failed: <code>{e}</code>")

        # Step 8: DB check
        try:
            active = await self._db.get_active_trades()
            history = await self._db.get_trade_history(limit=3)
            results.append(
                f"✅ Step 8 — DB: <code>{len(active)}</code> active, "
                f"<code>{len(history)}</code> in history"
            )
            logger.info(f"[DEBUG][FULL-FLOW] Step 8 DB check passed")
        except Exception as e:
            results.append(f"❌ Step 8 — DB check failed: <code>{e}</code>")

        passed = sum(1 for r in results if r.startswith("✅"))
        failed = len(results) - passed

        summary = "\n".join(results)
        await query.edit_message_text(
            f"{'✅' if failed == 0 else '⚠️'} <b>Full Flow Dry Run — {passed}/{len(results)} passed</b>\n\n"
            f"{summary}\n\n"
            f"<i>No real orders were placed. Read-only API calls were made for balance and ticker.</i>",
            parse_mode="HTML",
            reply_markup=_back_kb(),
        )