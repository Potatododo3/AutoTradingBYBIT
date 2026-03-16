"""
Soft Stop Loss Monitor
======================
Polls Bitunix kline data at candle-close boundaries and checks whether the
closing price has breached the soft SL level for any active trade.

Design:
  - One asyncio task per active timeframe (15m / 30m / 1h / 4h / 1d).
  - Wakes up just *after* each candle close (boundary + small buffer).
  - Fetches the just-closed candle for every pair using that timeframe.
  - Compares close price to trade.soft_sl_price.
  - LONG  → breach if close < soft_sl_price
  - SHORT → breach if close > soft_sl_price
  - On breach: sends a Telegram alert with Close / Ignore inline buttons.
  - Cooldown: once alerted for a pair+timeframe, won't re-alert for 2 candles
    (prevents spam if the trade is left open and price stays near SL).

Timeframe periods (seconds):
  15m  →   900
  30m  → 1 800
  1h   → 3 600
  4h   → 14 400
  1d   → 86 400
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from bybit_client import BybitClient
from models import Side, TradeRecord

logger = logging.getLogger(__name__)

# ── Timeframe config ──────────────────────────────────────────────────────────

TIMEFRAME_MAP: dict[str, str] = {
    "15m":   "15m",
    "30m":   "30m",
    "1h":    "1h",
    "4h":    "4h",
    "Daily": "1d",
}

PERIOD_SECONDS: dict[str, int] = {
    "15m":   900,
    "30m":   1_800,
    "1h":    3_600,
    "4h":    14_400,
    "Daily": 86_400,
}

# How many seconds after candle close to fetch (gives exchange time to finalize)
FETCH_BUFFER = 5

# Don't re-alert for this many candles after an alert was sent
COOLDOWN_CANDLES = 2


# ── Alert callback type ───────────────────────────────────────────────────────
# Called by the monitor when a candle closes beyond the soft SL.
# Signature: async def on_breach(trade, timeframe, close_price)
AlertCallback = Callable[[TradeRecord, str, float, int], None]  # trade, timeframe, close_price, candle_ts


@dataclass
class CooldownState:
    """Per (pair, timeframe) cooldown tracking."""
    alert_sent_at_candle: int = -1   # candle open-time of the alert candle
    ignore_until_candle:  int = -1   # candle open-time to resume checking


class SoftSLMonitor:
    """
    Runs background monitoring tasks for soft stop losses.
    Call start() to launch, stop() to shut down gracefully.
    """

    def __init__(
        self,
        client: BybitClient,
        get_active_trades: Callable[[], dict[str, TradeRecord]],
        on_breach: AlertCallback,
    ) -> None:
        self._client   = client
        self._get_trades = get_active_trades
        self._on_breach  = on_breach
        self._tasks: list[asyncio.Task] = []
        self._running = False
        # Cooldown per (pair, timeframe)
        self._cooldowns: dict[tuple[str, str], CooldownState] = {}

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Launch one monitoring task per timeframe."""
        if self._running:
            return
        self._running = True
        for tf in PERIOD_SECONDS:
            task = asyncio.create_task(
                self._monitor_loop(tf),
                name=f"soft_sl_{tf}",
            )
            self._tasks.append(task)
        logger.info(f"SoftSL monitor started for timeframes: {list(PERIOD_SECONDS)}")

    async def stop(self) -> None:
        """Cancel all monitoring tasks."""
        self._running = False
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("SoftSL monitor stopped.")

    def notify_cooldown_ignored(self, pair: str, timeframe: str, current_candle_ts: int) -> None:
        """Called by the handler when user taps 'Ignore' — sets cooldown."""
        key = (pair, timeframe)
        cd  = self._cooldowns.setdefault(key, CooldownState())
        cd.alert_sent_at_candle  = current_candle_ts
        cd.ignore_until_candle   = current_candle_ts + COOLDOWN_CANDLES * PERIOD_SECONDS[timeframe] * 1000
        logger.info(f"SoftSL cooldown set for {pair}/{timeframe} until {cd.ignore_until_candle}")

    def clear_cooldown(self, pair: str) -> None:
        """Clear all cooldowns for a pair (e.g. after trade is closed)."""
        to_remove = [k for k in self._cooldowns if k[0] == pair]
        for k in to_remove:
            del self._cooldowns[k]

    # ── Monitor loop ──────────────────────────────────────────────────────────

    async def _monitor_loop(self, timeframe: str) -> None:
        period = PERIOD_SECONDS[timeframe]
        logger.info(f"SoftSL [{timeframe}]: loop started, period={period}s")

        while self._running:
            try:
                sleep_secs = self._seconds_until_next_close(period)
                logger.debug(f"SoftSL [{timeframe}]: sleeping {sleep_secs:.1f}s until next candle close")
                await asyncio.sleep(sleep_secs)

                if not self._running:
                    break

                await self._check_all_trades(timeframe)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"SoftSL [{timeframe}] loop error: {e}", exc_info=True)
                await asyncio.sleep(30)   # back off on unexpected errors

    def _seconds_until_next_close(self, period: int) -> float:
        """
        Seconds until the next candle closes for this period, plus FETCH_BUFFER.
        E.g. for 1h at 13:47 UTC → next close at 14:00 → 13 min + buffer.
        """
        now_ts = time.time()
        # Align to period boundary
        next_boundary = (int(now_ts // period) + 1) * period
        return max(1.0, next_boundary - now_ts + FETCH_BUFFER)

    # ── Check all trades for this timeframe ───────────────────────────────────

    async def _check_all_trades(self, timeframe: str) -> None:
        trades = self._get_trades()
        if not trades:
            return

        interval = TIMEFRAME_MAP[timeframe]

        # Find trades that have a soft SL set for this timeframe
        relevant = [
            t for t in trades.values()
            if t.soft_sl_price and t.soft_sl_price > 0
            and t.soft_sl_timeframe == timeframe
        ]

        if not relevant:
            logger.debug(f"SoftSL [{timeframe}]: no trades monitoring this timeframe")
            return

        logger.info(f"SoftSL [{timeframe}]: checking {len(relevant)} trade(s)")

        for trade in relevant:
            try:
                await self._check_trade(trade, timeframe, interval)
            except Exception as e:
                logger.error(f"SoftSL [{timeframe}] check error for {trade.pair}: {e}")

    async def _check_trade(self, trade: TradeRecord, timeframe: str, interval: str) -> None:
        period  = PERIOD_SECONDS[timeframe]
        pair    = trade.pair
        key     = (pair, timeframe)

        # Fetch the last 2 candles (index -2 is the just-closed one, -1 is forming)
        candles = await self._client.get_klines(pair, interval, limit=2)
        if len(candles) < 1:
            logger.warning(f"SoftSL [{timeframe}] {pair}: no candles returned")
            return

        # The just-closed candle is the second-to-last (last is still forming)
        # If only one candle returned, use it
        closed_candle = candles[-2] if len(candles) >= 2 else candles[-1]
        close_price  = closed_candle["close"]
        candle_ts    = closed_candle["time"]   # open time in ms

        # Cooldown check
        cd = self._cooldowns.get(key)
        if cd and candle_ts <= cd.ignore_until_candle:
            logger.debug(f"SoftSL [{timeframe}] {pair}: in cooldown, skipping (candle_ts={candle_ts})")
            return

        # Check breach
        sl = trade.soft_sl_price
        breached = False
        if trade.side == Side.LONG  and close_price < sl:
            breached = True
        elif trade.side == Side.SHORT and close_price > sl:
            breached = True

        direction = "below" if trade.side == Side.LONG else "above"
        logger.info(
            f"SoftSL [{timeframe}] {pair}: close={close_price} sl={sl} "
            f"side={trade.side.value} breached={breached}"
        )

        if breached:
            logger.info(f"SoftSL BREACH [{timeframe}] {pair}: alerting user")
            await self._on_breach(trade, timeframe, close_price, candle_ts)