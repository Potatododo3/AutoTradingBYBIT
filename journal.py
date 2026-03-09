# Bybit Bot — journal.py
import asyncio
import csv
import logging
import os
from datetime import datetime
from typing import TYPE_CHECKING, Optional

import httpx

from config import DISCORD_WEBHOOK_URL, CSV_JOURNAL_PATH

if TYPE_CHECKING:
    from models import TradeRecord, PositionSizing

logger = logging.getLogger(__name__)

DISCORD_COLOR_GREEN = 0x00C853
DISCORD_COLOR_RED = 0xD50000
DISCORD_COLOR_BLUE = 0x2196F3
DISCORD_COLOR_ORANGE = 0xFF6F00
DISCORD_COLOR_GREY = 0x607D8B


class Journal:
    def __init__(self) -> None:
        self._http = httpx.AsyncClient(timeout=10.0)
        self._ensure_csv()

    def _ensure_csv(self) -> None:
        if not os.path.exists(CSV_JOURNAL_PATH):
            with open(CSV_JOURNAL_PATH, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "timestamp", "trade_id", "pair", "side", "entry", "exit",
                    "risk", "pnl", "r_multiple", "duration", "reason"
                ])

    def _write_csv(self, trade: "TradeRecord", exit_price: float = 0.0, reason: str = "") -> None:
        duration = ""
        if trade.closed_at and trade.opened_at:
            delta = trade.closed_at - trade.opened_at
            hours, rem = divmod(int(delta.total_seconds()), 3600)
            minutes = rem // 60
            duration = f"{hours}h {minutes}m"

        r_multiple = ""
        if trade.risk_amount and trade.risk_amount > 0:
            r_multiple = f"{trade.realized_pnl / trade.risk_amount:.2f}R"

        with open(CSV_JOURNAL_PATH, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                datetime.utcnow().isoformat(),
                trade.trade_id,
                trade.pair,
                trade.side.value,
                trade.entry,
                exit_price or trade.exit_price,
                f"${trade.risk_amount:.2f}",
                f"${trade.realized_pnl:.2f}",
                r_multiple,
                duration,
                reason,
            ])

    async def _send_webhook(self, payload: dict) -> None:
        if not DISCORD_WEBHOOK_URL:
            return
        try:
            await self._http.post(DISCORD_WEBHOOK_URL, json=payload)
        except Exception as e:
            logger.warning(f"Discord webhook failed: {e}")

    async def _fire_and_forget(self, payload: dict) -> None:
        """Directly await the webhook — all callers are already async."""
        await self._send_webhook(payload)

    def _embed(
        self,
        title: str,
        color: int,
        fields: list[dict],
        trade_id: str = "",
    ) -> dict:
        embed: dict = {
            "title": title,
            "color": color,
            "timestamp": datetime.utcnow().isoformat(),
            "fields": fields,
            "footer": {"text": f"Trade ID: {trade_id}" if trade_id else "Bitunix Trading Bot"},
        }
        return {"embeds": [embed]}

    def _field(self, name: str, value: str, inline: bool = True) -> dict:
        return {"name": name, "value": value, "inline": inline}

    async def log_trade_open(self, trade: "TradeRecord", sizing: "PositionSizing") -> None:
        payload = self._embed(
            title="🟢 NEW TRADE OPENED",
            color=DISCORD_COLOR_GREEN,
            trade_id=trade.trade_id,
            fields=[
                self._field("Pair", trade.pair),
                self._field("Side", trade.side.value.upper()),
                self._field("Entry Price", f"${sizing.entry_price:,.4f}"),
                self._field("Stop Loss", f"${trade.sl:,.4f}" if trade.sl else "—"),
                self._field("Risk Amount", f"${sizing.risk_amount:.2f}"),
                self._field("Risk %", f"{sizing.risk_percent:.2f}%"),
                self._field("Position Size", f"{trade.position_size}"),
                self._field("Leverage", f"{trade.leverage}x"),
                self._field("Margin Used", f"${sizing.margin_required:.2f}"),
                self._field("Account Balance", f"${sizing.balance:.2f}"),
                self._field("TP1", f"${trade.tp1:,.4f}" if trade.tp1 else "—"),
                self._field("TP2", f"${trade.tp2:,.4f}" if trade.tp2 else "—"),
                self._field("TP3", f"${trade.tp3:,.4f}" if trade.tp3 else "—"),
            ],
        )
        await self._fire_and_forget(payload)

    async def log_tp_hit(
        self,
        trade: "TradeRecord",
        tp_num: int,
        tp_price: float,
        qty_closed: float,
        remaining: float,
    ) -> None:
        """Fires when TP1, TP2, or TP3 order is detected as filled."""
        icons = {1: "🥇", 2: "🥈", 3: "🥉"}
        icon  = icons.get(tp_num, "✅")
        payload = self._embed(
            title=f"{icon} TP{tp_num} FILLED — {trade.pair}",
            color=DISCORD_COLOR_BLUE,
            trade_id=trade.trade_id,
            fields=[
                self._field("Pair",         trade.pair),
                self._field("Side",         trade.side.value.upper()),
                self._field("TP Level",     f"TP{tp_num}"),
                self._field("Fill Price",   f"${tp_price:,.4f}"),
                self._field("Qty Closed",   f"{qty_closed:.4f}"),
                self._field("Remaining",    f"{remaining:.4f}"),
                self._field("Entry",        f"${trade.entry:,.4f}"),
                self._field("Strategy",     trade.strategy.title() if trade.strategy else "—"),
            ],
        )
        await self._fire_and_forget(payload)
        self._write_csv(trade, exit_price=tp_price, reason=f"tp{tp_num}_hit")

    async def log_sl_moved_to_be(self, trade: "TradeRecord", old_sl: float) -> None:
        """Fires when SL is automatically or manually moved to breakeven (entry price)."""
        payload = self._embed(
            title=f"🔒 SL MOVED TO BREAKEVEN — {trade.pair}",
            color=DISCORD_COLOR_GREEN,
            trade_id=trade.trade_id,
            fields=[
                self._field("Pair",     trade.pair),
                self._field("Side",     trade.side.value.upper()),
                self._field("Old SL",   f"${old_sl:,.4f}"),
                self._field("New SL",   f"${trade.entry:,.4f}  (breakeven)"),
                self._field("Entry",    f"${trade.entry:,.4f}"),
                self._field("Trigger",  "Auto after TP1" if trade.tp1 else "Manual"),
            ],
        )
        await self._fire_and_forget(payload)

    async def log_sl_modified(self, trade: "TradeRecord", old_sl: float, new_sl: float) -> None:
        """Fires when SL is manually moved to any price."""
        payload = self._embed(
            title=f"✏️ SL MODIFIED — {trade.pair}",
            color=DISCORD_COLOR_ORANGE,
            trade_id=trade.trade_id,
            fields=[
                self._field("Pair",    trade.pair),
                self._field("Side",    trade.side.value.upper()),
                self._field("Old SL",  f"${old_sl:,.4f}"),
                self._field("New SL",  f"${new_sl:,.4f}"),
            ],
        )
        await self._fire_and_forget(payload)

    async def log_sl_hit(
        self,
        trade: "TradeRecord",
        close_price: float,
        pnl: float,
        fee: float = 0.0,
        funding: float = 0.0,
    ) -> None:
        """Fires when exchange SL order is detected as triggered."""
        r_multiple = f"{pnl / trade.risk_amount:.2f}R" if trade.risk_amount else "—"
        net_pnl    = pnl - abs(fee) - abs(funding)
        sign       = "+" if pnl >= 0 else ""
        duration   = ""
        if trade.closed_at and trade.opened_at:
            delta = trade.closed_at - trade.opened_at
            h, rem = divmod(int(delta.total_seconds()), 3600)
            duration = f"{h}h {rem // 60}m"
        payload = self._embed(
            title=f"🔴 STOP LOSS HIT — {trade.pair}",
            color=DISCORD_COLOR_RED,
            trade_id=trade.trade_id,
            fields=[
                self._field("Pair",         trade.pair),
                self._field("Side",         trade.side.value.upper()),
                self._field("Entry",        f"${trade.entry:,.4f}"),
                self._field("SL Price",     f"${trade.sl:,.4f}"),
                self._field("Close Price",  f"${close_price:,.4f}"),
                self._field("Realized PnL", f"{sign}${pnl:.2f}"),
                self._field("Net PnL",      f"{sign}${net_pnl:.2f}  (after fees)"),
                self._field("R-Multiple",   r_multiple),
                self._field("Duration",     duration or "—"),
                self._field("Strategy",     trade.strategy.title() if trade.strategy else "—"),
            ],
        )
        await self._fire_and_forget(payload)
        self._write_csv(trade, exit_price=close_price, reason="sl_hit")

    async def log_position_closed_externally(
        self,
        trade: "TradeRecord",
        close_price: float,
        pnl: float,
        reason: str,
        fee: float = 0.0,
        funding: float = 0.0,
    ) -> None:
        """
        Fires when a position disappears from exchange but wasn't closed by the bot.
        reason: 'tp3_hit' | 'manual_close' | 'liquidated' | 'unknown'
        """
        titles = {
            "tp3_hit":      f"🥉 TP3 HIT — {trade.pair}",
            "manual_close": f"🔒 CLOSED MANUALLY — {trade.pair}",
            "liquidated":   f"💀 LIQUIDATED — {trade.pair}",
            "unknown":      f"📭 POSITION CLOSED — {trade.pair}",
        }
        colors = {
            "tp3_hit":      DISCORD_COLOR_BLUE,
            "manual_close": DISCORD_COLOR_ORANGE,
            "liquidated":   DISCORD_COLOR_RED,
            "unknown":      DISCORD_COLOR_GREY,
        }
        net_pnl = pnl - abs(fee) - abs(funding)
        sign    = "+" if pnl >= 0 else ""
        r_mult  = f"{pnl / trade.risk_amount:.2f}R" if trade.risk_amount else "—"
        duration = ""
        if trade.closed_at and trade.opened_at:
            delta = trade.closed_at - trade.opened_at
            h, rem = divmod(int(delta.total_seconds()), 3600)
            duration = f"{h}h {rem // 60}m"
        payload = self._embed(
            title=titles.get(reason, titles["unknown"]),
            color=colors.get(reason, DISCORD_COLOR_GREY),
            trade_id=trade.trade_id,
            fields=[
                self._field("Pair",         trade.pair),
                self._field("Side",         trade.side.value.upper()),
                self._field("Entry",        f"${trade.entry:,.4f}"),
                self._field("Close Price",  f"${close_price:,.4f}"),
                self._field("Realized PnL", f"{sign}${pnl:.2f}"),
                self._field("Net PnL",      f"{sign}${net_pnl:.2f}  (after fees)"),
                self._field("R-Multiple",   r_mult),
                self._field("Duration",     duration or "—"),
                self._field("Strategy",     trade.strategy.title() if trade.strategy else "—"),
            ],
        )
        await self._fire_and_forget(payload)
        self._write_csv(trade, exit_price=close_price, reason=reason)

    async def log_trade_closed(self, trade: "TradeRecord", reason: str = "manual") -> None:
        duration = ""
        if trade.closed_at and trade.opened_at:
            delta = trade.closed_at - trade.opened_at
            hours, rem = divmod(int(delta.total_seconds()), 3600)
            minutes = rem // 60
            duration = f"{hours}h {minutes}m"

        exit_str = f"${trade.exit_price:,.4f}" if trade.exit_price else "N/A"
        pnl_sign = "+" if trade.realized_pnl >= 0 else ""
        payload = self._embed(
            title="🔒 MANUAL CLOSE" if reason == "manual" else "🔒 TRADE CLOSED",
            color=DISCORD_COLOR_ORANGE,
            trade_id=trade.trade_id,
            fields=[
                self._field("Pair", trade.pair),
                self._field("Entry", f"${trade.entry:,.4f}"),
                self._field("Exit", exit_str),
                self._field("Realized PnL", f"{pnl_sign}${trade.realized_pnl:.2f}"),
                self._field("Duration", duration or "N/A"),
            ],
        )
        await self._fire_and_forget(payload)
        self._write_csv(trade, reason=reason)

    async def log_closeall(self, trades: list["TradeRecord"]) -> None:
        pairs = ", ".join(t.pair for t in trades) if trades else "None"
        total_pnl = sum(t.realized_pnl for t in trades)
        payload = self._embed(
            title="🚨 EMERGENCY CLOSE ALL",
            color=DISCORD_COLOR_RED,
            fields=[
                self._field("Pairs Closed", pairs, inline=False),
                self._field("Total Realized PnL", f"${total_pnl:.2f}"),
            ],
        )
        await self._fire_and_forget(payload)

    async def log_soft_sl_breach(
        self,
        trade: "TradeRecord",
        timeframe: str,
        close_price: float,
    ) -> None:
        """Fires when a candle closes beyond the soft SL — position NOT closed yet."""
        from models import Side
        direction = "below" if trade.side == Side.LONG else "above"
        payload = self._embed(
            title="⚠️ SOFT SL BREACHED — NO CLOSE",
            color=DISCORD_COLOR_ORANGE,
            trade_id=trade.trade_id,
            fields=[
                self._field("Pair",        trade.pair),
                self._field("Candle Close", f"${close_price}"),
                self._field("Soft SL",     f"${trade.soft_sl_price}"),
                self._field("Timeframe",   timeframe),
                self._field("Entry",       f"${trade.entry}"),
                self._field(
                    "Status",
                    f"Candle closed {direction} soft SL — position still open, awaiting manual action.",
                    inline=False,
                ),
            ],
        )
        await self._fire_and_forget(payload)

    async def log_soft_sl_ignored(
        self,
        trade: "TradeRecord",
        timeframe: str,
    ) -> None:
        """Fires when the user taps Ignore on a soft SL breach alert."""
        payload = self._embed(
            title="⏩ SOFT SL IGNORED — POSITION STILL OPEN",
            color=DISCORD_COLOR_GREY,
            trade_id=trade.trade_id,
            fields=[
                self._field("Pair",      trade.pair),
                self._field("Soft SL",   f"${trade.soft_sl_price}"),
                self._field("Timeframe", timeframe),
                self._field("Entry",     f"${trade.entry}"),
                self._field(
                    "Status",
                    "Alert dismissed manually — position remains open, no close placed.",
                    inline=False,
                ),
            ],
        )
        await self._fire_and_forget(payload)

    async def log_rejection(self, pair: str, reason: str) -> None:
        payload = self._embed(
            title="⛔ TRADE REJECTED",
            color=DISCORD_COLOR_GREY,
            fields=[
                self._field("Pair", pair, inline=False),
                self._field("Reason", reason, inline=False),
            ],
        )
        await self._fire_and_forget(payload)

    async def log_error(self, error_type: str, pair: str, api_response: str = "") -> None:
        payload = self._embed(
            title="❌ ERROR",
            color=DISCORD_COLOR_RED,
            fields=[
                self._field("Error Type", error_type),
                self._field("Pair", pair),
                self._field("API Response", api_response[:500] if api_response else "N/A", inline=False),
                self._field("Timestamp", datetime.utcnow().isoformat()),
            ],
        )
        await self._fire_and_forget(payload)

    async def close(self) -> None:
        await self._http.aclose()