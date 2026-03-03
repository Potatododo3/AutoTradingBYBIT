"""
/settings — interactive configuration menu.

Settings are persisted in a JSON sidecar file (settings.json) next to the DB,
so they survive restarts without requiring a database migration.
All runtime code that reads config values should import from user_settings, not config.
"""
import json
import logging
import os
from dataclasses import asdict, dataclass

AWAITING_SETTING_INPUT = "awaiting_setting_input"
from typing import Any

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from config import (
    AUTHORIZED_USER_ID, DB_PATH,
    DEFAULT_LEVERAGE, MAX_LEVERAGE,
    TP1_PCT, TP2_PCT, TP3_PCT,
    DEBUG_MODE,
)

logger = logging.getLogger(__name__)

SETTINGS_PATH = os.path.join(os.path.dirname(DB_PATH), "settings.json")

DIV  = "─" * 28
HDIV = "═" * 28


# ── Settings dataclass ────────────────────────────────────────────────────────

@dataclass
class UserSettings:
    # Risk
    default_risk_pct:    float = 1.0       # default % risk per trade
    risk_balance:        float = 0.0       # fixed balance for risk calc; 0 = use live equity
    max_leverage:        int   = MAX_LEVERAGE  # always used — max leverage per trade

    # TP splits (must sum to 1.0)
    tp1_pct: float = TP1_PCT
    tp2_pct: float = TP2_PCT
    tp3_pct: float = TP3_PCT

    # Behaviour
    confirmation_required: bool = True
    close_all_confirmation: bool = True
    auto_move_sl_to_be:     bool = False   # move SL to breakeven after TP1
    trailing_sl:            bool = False   # trailing stop loss

    # Notifications
    notify_tp_hits:  bool = True
    notify_sl_hits:  bool = True
    notify_pnl_pct:  float = 5.0   # alert when unrealised PnL > X%

    # Display
    currency_symbol: str = "USDT"
    compact_mode:    bool = False   # shorter messages

    def save(self) -> None:
        with open(SETTINGS_PATH, "w") as f:
            json.dump(asdict(self), f, indent=2)
        logger.info(f"Settings saved to {SETTINGS_PATH}")

    @classmethod
    def load(cls) -> "UserSettings":
        if os.path.exists(SETTINGS_PATH):
            try:
                with open(SETTINGS_PATH) as f:
                    data = json.load(f)
                return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
            except Exception as e:
                logger.warning(f"Could not load settings: {e} — using defaults")
        return cls()


# Singleton — loaded once at startup, mutated in place, saved on change
_settings: UserSettings = UserSettings.load()


def get_settings() -> UserSettings:
    return _settings


def _reset_settings() -> None:
    """Reset singleton to defaults and save."""
    global _settings
    _settings = UserSettings()
    _settings.save()


# ── Keyboard builders ─────────────────────────────────────────────────────────

def _main_menu_kb() -> InlineKeyboardMarkup:
    s = _settings
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⚖️  Risk & Sizing",    callback_data="cfg:menu:risk"),
            InlineKeyboardButton("📐  TP Splits",         callback_data="cfg:menu:tp"),
        ],
        [
            InlineKeyboardButton("🔧  Behaviour",         callback_data="cfg:menu:behaviour"),
            InlineKeyboardButton("🔔  Notifications",     callback_data="cfg:menu:notify"),
        ],
        [
            InlineKeyboardButton("🖥  Display",           callback_data="cfg:menu:display"),
            InlineKeyboardButton("↩️  Reset to defaults", callback_data="cfg:reset"),
        ],
    ])


def _back_kb(to: str = "main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("◀  Back", callback_data=f"cfg:menu:{to}")
    ]])


def _toggle_btn(label: str, value: bool, key: str) -> InlineKeyboardButton:
    state = "✅ ON" if value else "⬜ OFF"
    return InlineKeyboardButton(f"{label}  {state}", callback_data=f"cfg:toggle:{key}")


def _inc_dec_row(label: str, key: str, value: Any, step: Any, fmt: str = ".0f") -> list[InlineKeyboardButton]:
    return [
        InlineKeyboardButton(f"−  {label}", callback_data=f"cfg:dec:{key}:{step}"),
        InlineKeyboardButton(f"{value:{fmt}}", callback_data="cfg:noop"),
        InlineKeyboardButton(f"+  {label}", callback_data=f"cfg:inc:{key}:{step}"),
    ]


# ── Menu text builders ────────────────────────────────────────────────────────

def _main_text() -> str:
    s = _settings
    be_str = "✅ ON" if s.auto_move_sl_to_be else "⬜ OFF"
    return (
        f"<b>SETTINGS</b>\n"
        f"<code>{HDIV}</code>\n\n"
        f"  <b>Risk</b>\n"
        f"  <code>{'Default risk':<18}</code>  {s.default_risk_pct:.1f}%\n"
        f"  <code>{'Risk balance':<18}</code>  {'live equity' if s.risk_balance <= 0 else f'${s.risk_balance:,.2f}'}\n"
        f"  <b>TP Splits</b>\n"
        f"  <code>{'TP1 / TP2 / TP3':<18}</code>  "
        f"{int(s.tp1_pct*100)}% / {int(s.tp2_pct*100)}% / {int(s.tp3_pct*100)}%\n\n"
        f"  <b>Behaviour</b>\n"
        f"  <code>{'Confirmation':<18}</code>  {'✅ ON' if s.confirmation_required else '⬜ OFF'}\n"
        f"  <code>{'Auto SL → BE':<18}</code>  {be_str}\n\n"
        f"  <b>Notifications</b>\n"
        f"  <code>{'TP alerts':<18}</code>  {'✅ ON' if s.notify_tp_hits else '⬜ OFF'}\n"
        f"  <code>{'SL alerts':<18}</code>  {'✅ ON' if s.notify_sl_hits else '⬜ OFF'}\n\n"
        f"  <i>Tap a section to edit.</i>"
    )


def _risk_text() -> str:
    s = _settings
    return (
        f"<b>RISK &amp; SIZING</b>\n"
        f"<code>{HDIV}</code>\n\n"
        f"  <code>{'Default risk %':<18}</code>  <b>{s.default_risk_pct:.1f}%</b>\n"
        f"  <i>Pre-filled in trade wizard</i>\n\n"
        f"  <code>{'Risk balance':<18}</code>  <b>{'live equity' if s.risk_balance <= 0 else f'${s.risk_balance:,.2f}'}</b>\n"
        f"  <i>Fixed $ base for risk calc — 0 = use live equity</i>\n\n"
        f"  <i>Use /setleverage PAIR N to set per-symbol leverage.</i>"
    )


def _risk_kb() -> InlineKeyboardMarkup:
    s = _settings
    rb_label = f"${s.risk_balance:,.0f}" if s.risk_balance > 0 else "live"
    return InlineKeyboardMarkup([
        _inc_dec_row("Default risk %", "default_risk_pct", s.default_risk_pct, 0.5, ".1f"),
        [
            InlineKeyboardButton(f"Risk balance: {rb_label}", callback_data="cfg:noop"),
        ],
        [
            InlineKeyboardButton("✏️  Type value", callback_data="cfg:input:risk_balance"),
            InlineKeyboardButton("Use live equity", callback_data="cfg:set:risk_balance:0"),
        ],
        [InlineKeyboardButton("◀  Back", callback_data="cfg:menu:main")],
    ])


def _tp_text() -> str:
    s  = _settings
    t1 = int(s.tp1_pct * 100)
    t2 = int(s.tp2_pct * 100)
    t3 = int(s.tp3_pct * 100)
    total = t1 + t2 + t3
    warn  = f"\n  {chr(9888)} <b>WARNING: splits sum to {total}% (must be 100%)</b>" if total != 100 else ""
    bar1 = "█" * (t1 // 5)
    bar2 = "█" * (t2 // 5)
    bar3 = "█" * (t3 // 5)
    return (
        f"<b>TAKE PROFIT SPLITS</b>\n"
        f"<code>{HDIV}</code>\n\n"
        f"  TP1  {t1:>3}%  <code>{bar1:<20}</code>\n"
        f"  TP2  {t2:>3}%  <code>{bar2:<20}</code>\n"
        f"  TP3  {t3:>3}%  <code>{bar3:<20}</code>\n"
        f"<code>{DIV}</code>\n"
        f"  Total       <b>{total}%</b>{warn}\n\n"
        f"  <i>Percentage of position closed at each TP.</i>"
    )


def _tp_kb() -> InlineKeyboardMarkup:
    s = _settings
    return InlineKeyboardMarkup([
        _inc_dec_row("TP1 %", "tp1_pct_int", int(s.tp1_pct * 100), 5, ".0f"),
        _inc_dec_row("TP2 %", "tp2_pct_int", int(s.tp2_pct * 100), 5, ".0f"),
        _inc_dec_row("TP3 %", "tp3_pct_int", int(s.tp3_pct * 100), 5, ".0f"),
        [InlineKeyboardButton("◀  Back", callback_data="cfg:menu:main")],
    ])


def _behaviour_text() -> str:
    s = _settings
    return (
        f"<b>BEHAVIOUR</b>\n"
        f"<code>{HDIV}</code>\n\n"
        f"  <code>{'Trade confirm':<22}</code>  {'✅ ON' if s.confirmation_required else '⬜ OFF'}\n"
        f"  <i>Show summary + confirm button before placing</i>\n\n"
        f"  <code>{'Close-all confirm':<22}</code>  {'✅ ON' if s.close_all_confirmation else '⬜ OFF'}\n"
        f"  <i>Require confirmation for /closeall</i>\n\n"
        f"  <code>{'Auto SL → Breakeven':<22}</code>  {'✅ ON' if s.auto_move_sl_to_be else '⬜ OFF'}\n"
        f"  <i>Move SL to entry price after TP1 hits</i>\n\n"
        f"  <code>{'Trailing SL':<22}</code>  {'✅ ON' if s.trailing_sl else '⬜ OFF'}\n"
        f"  <i>Trail SL as price moves in your favour</i>"
    )


def _behaviour_kb() -> InlineKeyboardMarkup:
    s = _settings
    return InlineKeyboardMarkup([
        [_toggle_btn("Trade confirm",     s.confirmation_required,  "confirmation_required")],
        [_toggle_btn("Close-all confirm", s.close_all_confirmation, "close_all_confirmation")],
        [_toggle_btn("Auto SL → BE",      s.auto_move_sl_to_be,     "auto_move_sl_to_be")],
        [_toggle_btn("Trailing SL",       s.trailing_sl,             "trailing_sl")],
        [InlineKeyboardButton("◀  Back", callback_data="cfg:menu:main")],
    ])


def _notify_text() -> str:
    s = _settings
    return (
        f"<b>NOTIFICATIONS</b>\n"
        f"<code>{HDIV}</code>\n\n"
        f"  <code>{'TP hit alerts':<20}</code>  {'✅ ON' if s.notify_tp_hits else '⬜ OFF'}\n"
        f"  <i>Notify when a take profit fills</i>\n\n"
        f"  <code>{'SL hit alerts':<20}</code>  {'✅ ON' if s.notify_sl_hits else '⬜ OFF'}\n"
        f"  <i>Notify when stop loss fills</i>\n\n"
        f"  <code>{'PnL alert %':<20}</code>  <b>{s.notify_pnl_pct:.1f}%</b>\n"
        f"  <i>Alert when unrealised PnL crosses ±this %</i>"
    )


def _notify_kb() -> InlineKeyboardMarkup:
    s = _settings
    return InlineKeyboardMarkup([
        [_toggle_btn("TP hit alerts", s.notify_tp_hits, "notify_tp_hits")],
        [_toggle_btn("SL hit alerts", s.notify_sl_hits, "notify_sl_hits")],
        _inc_dec_row("PnL alert %", "notify_pnl_pct", s.notify_pnl_pct, 1.0, ".1f"),
        [InlineKeyboardButton("◀  Back", callback_data="cfg:menu:main")],
    ])


def _display_text() -> str:
    s = _settings
    return (
        f"<b>DISPLAY</b>\n"
        f"<code>{HDIV}</code>\n\n"
        f"  <code>{'Compact mode':<20}</code>  {'✅ ON' if s.compact_mode else '⬜ OFF'}\n"
        f"  <i>Shorter messages, less detail</i>\n\n"
        f"  <code>{'Currency':<20}</code>  <b>{s.currency_symbol}</b>\n"
        f"  <i>Display label (cosmetic only)</i>"
    )


def _display_kb() -> InlineKeyboardMarkup:
    s = _settings
    return InlineKeyboardMarkup([
        [_toggle_btn("Compact mode", s.compact_mode, "compact_mode")],
        [InlineKeyboardButton("◀  Back", callback_data="cfg:menu:main")],
    ])


# ── Handler class ─────────────────────────────────────────────────────────────

class SettingsHandler:
    def __init__(self) -> None:
        pass

    async def _handle_text_input(self, update, context) -> None:
        """
        Catches a plain text message when the user was prompted to type
        a setting value (e.g. risk_balance). Ignores all other messages.
        """
        key = context.user_data.pop(AWAITING_SETTING_INPUT, None)
        if not key:
            return  # not waiting for input — ignore

        # Auth check
        uid = update.effective_user.id if update.effective_user else None
        if uid != AUTHORIZED_USER_ID:
            return

        text = update.message.text.strip().replace(',', '').replace('$', '')
        try:
            value = float(text)
        except ValueError:
            await update.message.reply_text(
                f"<b>INVALID</b>  <code>{text}</code> is not a number. Try again via /settings.",
                parse_mode="HTML",
            )
            return

        # Clamp to bounds
        bounds = {
            "risk_balance":     (0.0, 1_000_000.0),
            "default_risk_pct": (0.1, 20.0),
        }
        lo, hi = bounds.get(key, (0.0, 1_000_000.0))
        value  = max(lo, min(hi, value))

        if isinstance(getattr(_settings, key, 0.0), int):
            value = int(round(value))

        setattr(_settings, key, value)
        _settings.save()

        label = f"${value:,.2f}" if key == "risk_balance" and value > 0 else str(value)
        if key == "risk_balance" and value == 0:
            label = "live equity"

        await update.message.reply_text(
            f"<b>SAVED</b>  ✅\n\n"
            f"  <code>{key.replace('_', ' ')}</code>  →  <b>{label}</b>\n\n"
            f"  <i>Open /settings to continue editing.</i>",
            parse_mode="HTML",
        )
        logger.info(f"Setting {key} updated via text input → {value}")

    def register(self, app: Application) -> None:
        app.add_handler(CommandHandler("settings", self.cmd_settings))
        app.add_handler(CallbackQueryHandler(self._handle, pattern="^cfg:"))
        # Catch free-text input when user is prompted to type a setting value
        app.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            self._handle_text_input,
        ), group=1)

    def _auth(self, update: Update) -> bool:
        uid = update.effective_user.id if update.effective_user else None
        return uid == AUTHORIZED_USER_ID

    async def cmd_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update):
            await update.message.reply_text("⛔ Access denied.")
            return
        await update.message.reply_text(
            _main_text(),
            parse_mode="HTML",
            reply_markup=_main_menu_kb(),
        )

    async def _handle(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update):
            await update.callback_query.answer("⛔ Access denied.", show_alert=True)
            return

        query = update.callback_query
        await query.answer()
        action = query.data  # e.g. "cfg:menu:risk" or "cfg:toggle:confirmation_required"

        try:
            parts = action.split(":")

            # Navigation
            if parts[1] == "menu":
                page = parts[2] if len(parts) > 2 else "main"
                await self._show_page(query, page)

            # Toggle boolean
            elif parts[1] == "toggle":
                key = parts[2]
                if hasattr(_settings, key) and isinstance(getattr(_settings, key), bool):
                    setattr(_settings, key, not getattr(_settings, key))
                    _settings.save()
                # Re-render whichever page owns this toggle
                page = self._page_for_key(key)
                await self._show_page(query, page)

            # Increment / decrement numeric value
            elif parts[1] in ("inc", "dec"):
                key  = parts[2]
                step = float(parts[3])
                if parts[1] == "dec":
                    step = -step
                await self._adjust(query, key, step)

            # Adjust by signed delta (cfg:adj:key:delta) — used by risk_balance buttons
            elif parts[1] == "adj":
                key   = parts[2]
                delta = float(parts[3])
                await self._adjust(query, key, delta)

            # Direct set to value (cfg:set:key:value) — e.g. 'live' resets risk_balance to 0
            elif parts[1] == "set":
                key = parts[2]
                val = parts[3]
                if hasattr(_settings, key):
                    current = getattr(_settings, key)
                    if isinstance(current, int):
                        setattr(_settings, key, int(float(val)))
                    else:
                        setattr(_settings, key, float(val))
                    _settings.save()
                    page = self._page_for_key(key)
                    await self._show_page(query, page)

            # Reset all to defaults
            elif parts[1] == "reset":
                _reset_settings()
                await query.edit_message_text(
                    f"<b>SETTINGS RESET</b>\n"
                    f"<code>{HDIV}</code>\n\n"
                    f"  All settings restored to defaults.",
                    parse_mode="HTML",
                    reply_markup=_main_menu_kb(),
                )

            # No-op (tapping the value label in inc/dec row)
            elif parts[1] == "noop":
                pass

            # Text input prompt — bot will catch the next message as the value
            elif parts[1] == "input":
                key = parts[2]
                context.user_data[AWAITING_SETTING_INPUT] = key
                await query.edit_message_text(
                    f"<b>ENTER VALUE</b>\n"
                    f"<code>{'─' * 28}</code>\n\n"
                    f"  Type the new value for <b>{key.replace('_', ' ')}</b>\n"
                    f"  and send it as a message.\n\n"
                    f"  <i>e.g.</i>  <code>222.70</code>\n\n"
                    f"  Send <code>0</code> to use live equity.\n"
                    f"  Send /cancel to abort.",
                    parse_mode="HTML",
                )

        except Exception as e:
            logger.error(f"Settings handler error: {e}", exc_info=True)
            await query.edit_message_text(
                f"<b>ERROR</b>\n\n<code>{e}</code>",
                parse_mode="HTML",
                reply_markup=_back_kb("main"),
            )

    async def _show_page(self, query, page: str) -> None:
        pages = {
            "main":      (_main_text,      _main_menu_kb),
            "risk":      (_risk_text,      _risk_kb),
            "tp":        (_tp_text,        _tp_kb),
            "behaviour": (_behaviour_text, _behaviour_kb),
            "notify":    (_notify_text,    _notify_kb),
            "display":   (_display_text,   _display_kb),
        }
        text_fn, kb_fn = pages.get(page, pages["main"])
        await query.edit_message_text(
            text_fn(),
            parse_mode="HTML",
            reply_markup=kb_fn(),
        )

    async def _adjust(self, query, key: str, step: float) -> None:
        # Special virtual keys for TP percentages (stored as 0-100 int, saved as float 0-1)
        if key in ("tp1_pct_int", "tp2_pct_int", "tp3_pct_int"):
            real_key = key.replace("_int", "")
            current  = round(getattr(_settings, real_key) * 100)
            new_val  = max(0, min(100, current + int(step)))
            setattr(_settings, real_key, new_val / 100)
            _settings.save()
            await self._show_page(query, "tp")
            return

        # Numeric settings with bounds
        bounds = {
            "default_risk_pct":  (0.1, 20.0),
            "risk_balance":      (0.0, 1_000_000.0),
            "notify_pnl_pct":    (0.5, 50.0),
        }
        if hasattr(_settings, key):
            lo, hi   = bounds.get(key, (0, 999999))
            current  = getattr(_settings, key)
            new_val  = max(lo, min(hi, current + step))
            # Keep int type for int fields
            if isinstance(current, int):
                new_val = int(round(new_val))
            setattr(_settings, key, new_val)
            _settings.save()
            page = self._page_for_key(key)
            await self._show_page(query, page)

    @staticmethod
    def _page_for_key(key: str) -> str:
        risk_keys     = {"default_risk_pct","risk_balance"}
        tp_keys       = {"tp1_pct","tp2_pct","tp3_pct","tp1_pct_int","tp2_pct_int","tp3_pct_int"}
        behaviour_keys= {"confirmation_required","close_all_confirmation","auto_move_sl_to_be","trailing_sl"}
        notify_keys   = {"notify_tp_hits","notify_sl_hits","notify_pnl_pct"}
        display_keys  = {"compact_mode","currency_symbol"}
        if key in risk_keys:      return "risk"
        if key in tp_keys:        return "tp"
        if key in behaviour_keys: return "behaviour"
        if key in notify_keys:    return "notify"
        if key in display_keys:   return "display"
        return "main"