"""
OrderManager — trade lifecycle: open, close, sync, cancel.

TP/SL strategy:
  - Uses Bybit's trading-stop endpoint for SL. positionIdx stored as position_id.
  - TP1 and TP2 are placed as regular limit reduce-only orders (partial closes).

Manual trade sync (/sync):
  - Reads all open positions from the exchange.
  - For each untracked position, reads TPSL + pending limit orders to
    reconstruct SL/TP levels, then saves to DB.
"""
import asyncio
import logging
from datetime import datetime
from typing import Optional

from bybit_client import BybitClient
from database import Database
from journal import Journal
from models import (
    TradeRequest, PositionSizing, TradeRecord, TradeStatus,
    Side, DuplicateTradeError, APIError,
)
from settings_handler import get_settings
from utils import generate_trade_id

logger = logging.getLogger(__name__)


def _f(val, default: float = 0.0) -> float:
    """Safe float parse — returns default on None / empty / "0"."""
    try:
        return float(val or 0)
    except (TypeError, ValueError):
        return default


def _is_long(side: str) -> bool:
    """Normalise LONG/SHORT or BUY/SELL to a boolean."""
    return side.upper().strip() in ("LONG", "BUY")


def _detect_strategy(entry_qty: float, dca_qty: float, buffer: float = 0.07) -> Optional[str]:
    """Detect neil (2x) or saltwayer (2.5x) from DCA/entry qty ratio with 7% buffer."""
    if not entry_qty or not dca_qty:
        return None
    ratio = dca_qty / entry_qty
    if abs(ratio - 2.0) <= buffer:
        return "neil"
    if abs(ratio - 2.5) <= buffer:
        return "saltwayer"
    return None


def _tpsl_price(order: dict, key: str) -> float:
    """Extract a price from a TPSL order, treating '0'/''/None as not-set."""
    raw = order.get(key, None)
    if raw in (None, "", "0", 0):
        return 0.0
    return _f(raw)


def _safe_tp_qtys(
    position_size: float, tp1_pct: float, tp2_pct: float, qp: int, min_qty: float
) -> tuple[float, float, float]:
    """
    Split position_size into (tp1, tp2, tp3) quantities.
    If any slice rounds below min_qty, it is absorbed into the previous TP
    so no order is placed below the exchange minimum.
    """
    tp1 = round(position_size * tp1_pct, qp)
    tp2 = round(position_size * tp2_pct, qp)
    tp3 = round(position_size - tp1 - tp2, qp)
    if tp3 < min_qty:
        tp2 = round(tp2 + tp3, qp)
        tp3 = 0.0
    if tp2 < min_qty:
        tp1 = round(tp1 + tp2, qp)
        tp2 = 0.0
    return tp1, tp2, tp3


class OrderManager:
    def __init__(self, client: BybitClient, db: Database, journal: "Journal") -> None:
        self._client = client
        self._db = db
        self._jnl = journal
        self._cache: dict[str, TradeRecord] = {}
        self._seen_close_orders: set[str] = set()  # in-memory set of processed close order IDs

    async def load_from_db(self) -> None:
        self._cache = await self._db.get_active_trades()
        if self._cache:
            logger.info(f"Restored {len(self._cache)} trade(s): {', '.join(self._cache)}")
            # Restore seen_close_orders from DB so partial-close detections survive restarts
            for trade in self._cache.values():
                if trade.seen_close_ids:
                    ids = {s for s in trade.seen_close_ids.split(",") if s}
                    self._seen_close_orders.update(ids)
            if self._seen_close_orders:
                logger.info(f"Restored {len(self._seen_close_orders)} seen close order ID(s) from DB")
        else:
            logger.info("No active trades in DB.")
        # Reconcile any trades that had unfilled limit entries when bot was last stopped
        await self._reconcile_pending_entries()
        # Reconcile any trades that were closed externally while bot was offline
        await self._reconcile_closed_externally()
        # Backfill exit prices for old closed trades that were saved with exit_price=0
        await self._backfill_missing_exit_prices()


    def has_active_trade(self, pair: str) -> bool:
        return pair in self._cache

    def get_active_trade(self, pair: str) -> Optional[TradeRecord]:
        return self._cache.get(pair)

    def get_all_active(self) -> dict[str, TradeRecord]:
        return dict(self._cache)

    # ── Open trade ────────────────────────────────────────────────────────────

    async def open_trade(self, req: TradeRequest, sizing: PositionSizing) -> TradeRecord:
        if self.has_active_trade(req.pair):
            raise DuplicateTradeError(f"Already tracking a trade on {req.pair}.")

        s = get_settings()
        trade_id = generate_trade_id()
        pair = req.pair

        await self._client.set_leverage(pair, sizing.leverage)

        entry_side = "BUY" if req.side == Side.LONG else "SELL"
        close_side = "SELL" if req.side == Side.LONG else "BUY"

        if req.is_market:
            entry_order_id = await self._client.place_order(
                symbol=pair, side=entry_side, order_type="MARKET",
                qty=sizing.position_size, trade_side="OPEN",
            )
        else:
            entry_order_id = await self._client.place_order(
                symbol=pair, side=entry_side, order_type="LIMIT",
                qty=sizing.position_size, price=sizing.entry_price, trade_side="OPEN",
            )

        # ── Get positionId ────────────────────────────────────────────────────
        # Market orders fill instantly — position should appear within 1-2s.
        # Limit orders won't fill until price hits, so position may not exist yet.
        # We retry up to ~5s for market; give up gracefully for limit (user
        # can run /sync once the order fills to attach TPs).
        position_id = ""
        max_attempts = 5 if req.is_market else 2
        for attempt in range(max_attempts):
            await asyncio.sleep(1.0)
            position = await self._client.get_position(pair)
            if position and position.position_id:
                position_id = position.position_id
                logger.info(f"Got positionId={position_id} for {pair} (attempt {attempt+1})")
                break
            logger.debug(f"positionId not yet available for {pair} (attempt {attempt+1}/{max_attempts})")

        if not position_id:
            if req.is_market:
                logger.warning(f"Market order for {pair} — no positionId after {max_attempts}s, TP/SL will be skipped")
            else:
                logger.info(f"Limit order for {pair} not yet filled — TP/SL will be placed after fill via /sync")

        # ── TP quantities ─────────────────────────────────────────────────────
        qp = sizing.qty_precision
        min_qty = sizing.min_qty
        tp1_qty, tp2_qty, tp3_qty = _safe_tp_qtys(
            sizing.position_size, s.tp1_pct, s.tp2_pct, qp, min_qty
        )

        # ── Place TP1 / TP2 limit orders ──────────────────────────────────────
        tp1_order_id = ""
        tp2_order_id = ""
        tpsl_id      = ""

        if position_id:
            tp1_order_id = ""
            if req.tp1 and req.tp1 > 0:
                tp1_order_id = await self._client.place_order(
                    symbol=pair, side=close_side, order_type="LIMIT",
                    qty=tp1_qty, price=req.tp1, reduce_only=True, trade_side="CLOSE",
                    position_id=position_id,
                )
            tp2_order_id = ""
            if tp2_qty > 0 and req.tp2 and req.tp2 > 0:
                tp2_order_id = await self._client.place_order(
                    symbol=pair, side=close_side, order_type="LIMIT",
                    qty=tp2_qty, price=req.tp2, reduce_only=True, trade_side="CLOSE",
                    position_id=position_id,
                )
            tp3_order_id_val = ""
            if tp3_qty > 0 and req.tp3 and req.tp3 > 0:
                tp3_order_id_val = await self._client.place_order(
                    symbol=pair, side=close_side, order_type="LIMIT",
                    qty=tp3_qty, price=req.tp3, reduce_only=True, trade_side="CLOSE",
                    position_id=position_id,
                )
            if req.sl and req.sl > 0:
                tpsl_id = await self._client.place_tpsl(
                    symbol=pair, position_id=position_id, sl_price=req.sl,
                )
            else:
                tpsl_id = ""
        else:
            tp3_order_id_val = ""
            tpsl_id = ""
            logger.info(f"Limit entry for {pair} not yet filled — TPs placed automatically when entry fills.")

        dca_order_id: Optional[str] = None
        if req.dca is not None:
            if req.dca_qty:  # dca_qty is the multiplier (2.0 or 2.5)
                dca_qty = round(sizing.position_size * req.dca_qty, sizing.qty_precision)
            else:
                dca_qty = sizing.position_size  # same size as entry
            dca_order_id = await self._client.place_order(
                symbol=pair, side=entry_side, order_type="LIMIT",
                qty=dca_qty, price=req.dca, trade_side="OPEN",
            )

        trade = TradeRecord(
            trade_id=trade_id, pair=pair, side=req.side,
            entry=sizing.entry_price, sl=req.sl,
            tp1=req.tp1, tp2=req.tp2, tp3=req.tp3,
            position_size=sizing.position_size, leverage=sizing.leverage,
            risk_amount=sizing.risk_amount, balance_at_entry=sizing.balance,
            dca=req.dca,
            strategy=req.strategy,
            entry_order_id=entry_order_id,
            sl_order_id=tpsl_id,
            tp1_order_id=tp1_order_id,
            tp2_order_id=tp2_order_id,
            tp3_order_id=tp3_order_id_val if position_id else "",
            dca_order_id=dca_order_id,
            position_id=position_id,
        )

        await self._db.insert_trade(trade)
        self._cache[pair] = trade
        await self._jnl.log_trade_open(trade, sizing)
        logger.info(f"[{trade_id}] Opened {pair} {req.side.value} posId={position_id}")
        return trade

    # ── Reconcile pending limit entries on startup ──────────────────────────
    # When the bot restarts, any trade with position_id="" and tp1_order_id=""
    # had its limit entry unfilled at shutdown time. We check the entry order:
    #   FILLED     → position now exists, place TPs now
    #   PART_FILLED→ position partially exists, still place TPs on actual qty
    #   CANCELLED  → entry was cancelled externally, remove from DB
    #   NEW/live   → entry still pending, leave as-is (user can cancel or wait)


    async def _backfill_missing_exit_prices(self) -> None:
        """
        One-time backfill: find closed trades with exit_price=0 and re-fetch
        exit price from order history. Runs at startup after load_from_db.
        """
        trades = await self._db.get_trades_missing_exit()
        if not trades:
            return
        logger.info(f"Backfill: {len(trades)} closed trade(s) missing exit price — fetching from history...")
        patched = 0
        for t in trades:
            try:
                start_ms = int(t.opened_at.timestamp() * 1000)
                hist_orders = await self._client.get_history_orders(
                    symbol=t.pair, limit=20, start_time=start_ms,
                )
                close_orders = [
                    o for o in hist_orders
                    if str(o.get("tradeSide", "")).upper() == "CLOSE"
                    and str(o.get("status", "")).rstrip("_").upper() in ("FILLED", "PART_FILLED")
                ]
                if not close_orders:
                    logger.debug(f"Backfill [{t.trade_id}] {t.pair}: no close orders found in history")
                    continue
                close_orders.sort(
                    key=lambda o: int(o.get("ctime", o.get("mtime", 0)) or 0),
                    reverse=True,
                )
                last = close_orders[0]
                exit_price = float(last.get("price", 0) or 0)
                fee        = float(last.get("fee", 0) or 0)
                pnl        = float(last.get("realizedPNL", 0) or 0) - abs(fee)
                if exit_price:
                    await self._db.update_exit_price(t.trade_id, exit_price, pnl)
                    logger.info(f"Backfill [{t.trade_id}] {t.pair}: exit={exit_price} pnl={pnl:.2f}")
                    patched += 1
            except Exception as e:
                logger.warning(f"Backfill [{t.trade_id}] {t.pair}: failed — {e}")
        if patched:
            logger.info(f"Backfill complete: patched {patched}/{len(trades)} trade(s)")

    async def _reconcile_pending_entries(self) -> None:
        """
        Called once on startup. For each tracked trade that has no positionId
        (limit entry was pending when bot shut down), check what happened to
        the entry order while the bot was offline and react accordingly.
        """
        s = get_settings()
        pending = [
            t for t in self._cache.values()
            if not t.position_id and t.entry_order_id
        ]
        if not pending:
            return

        logger.info(f"Reconciling {len(pending)} pending limit entr(ies) after restart...")

        for trade in pending:
            pair = trade.pair
            try:
                order = await self._client.get_order_status(trade.entry_order_id)
                status = order.get("status", "UNKNOWN")
                logger.info(f"[{trade.trade_id}] Entry order {trade.entry_order_id} status: {status}")

                if status in ("FILLED", "PART_FILLED"):
                    # Position should now exist — get positionId and place TPs
                    await asyncio.sleep(0.5)
                    position = await self._client.get_position(pair)
                    if not position or not position.position_id:
                        logger.warning(f"[{trade.trade_id}] Order {status} but no position found for {pair} — skipping")
                        continue

                    position_id = position.position_id
                    trade.position_id = position_id
                    # Use actual filled qty from position, not original sizing
                    actual_qty = position.size

                    close_side = "SELL" if trade.side == Side.LONG else "BUY"
                    sym_info_r = await self._client.get_symbol_info(pair)
                    min_qty_r  = sym_info_r["minTradeVolume"]
                    tp1_qty, tp2_qty, tp3_qty = _safe_tp_qtys(
                        actual_qty, s.tp1_pct, s.tp2_pct,
                        sym_info_r["basePrecision"], min_qty_r
                    )

                    if trade.tp1 and trade.tp1 > 0:
                        trade.tp1_order_id = await self._client.place_order(
                            symbol=pair, side=close_side, order_type="LIMIT",
                            qty=tp1_qty, price=trade.tp1, reduce_only=True,
                            trade_side="CLOSE", position_id=position_id,
                        )
                    if tp2_qty > 0 and trade.tp2 and trade.tp2 > 0:
                        trade.tp2_order_id = await self._client.place_order(
                            symbol=pair, side=close_side, order_type="LIMIT",
                            qty=tp2_qty, price=trade.tp2, reduce_only=True,
                            trade_side="CLOSE", position_id=position_id,
                        )
                    if tp3_qty > 0 and trade.tp3 and trade.tp3 > 0:
                        trade.tp3_order_id = await self._client.place_order(
                            symbol=pair, side=close_side, order_type="LIMIT",
                            qty=tp3_qty, price=trade.tp3, reduce_only=True,
                            trade_side="CLOSE", position_id=position_id,
                        )
                    if trade.sl and trade.sl > 0:
                        trade.sl_order_id = await self._client.place_tpsl(
                            symbol=pair, position_id=position_id, sl_price=trade.sl,
                        )

                    await self._db.update_trade(trade)
                    logger.info(
                        f"[{trade.trade_id}] Reconciled {pair}: "
                        f"posId={position_id} tp1={trade.tp1_order_id} "
                        f"tp2={trade.tp2_order_id} tpsl={trade.sl_order_id}"
                    )

                elif status in ("CANCELLED", "CANCELED"):
                    # Entry was cancelled externally while bot was down — remove from tracking
                    logger.warning(f"[{trade.trade_id}] Entry order cancelled externally — removing {pair} from tracking")
                    await self._db.mark_trade_cancelled(trade.trade_id)
                    self._cache.pop(pair, None)

                else:
                    # NEW, PENDING, PART_FILLED with no position yet — still live, leave it
                    logger.info(f"[{trade.trade_id}] Entry order still live ({status}) for {pair} — no action needed")

            except Exception as e:
                logger.error(f"[{trade.trade_id}] Reconcile error for {pair}: {e}", exc_info=True)

    # ── Reconcile externally-closed trades on startup ──────────────────────────
    # Fetches all live positions from the exchange and marks any tracked trade
    # as closed if its position is no longer present. Handles trades closed
    # via the app, web UI, TP hit, SL hit, or liquidation while bot was offline.

    async def _reconcile_closed_externally(self) -> None:
        if not self._cache:
            return
        try:
            positions = await self._client.get_positions()
        except Exception as e:
            logger.warning(f"_reconcile_closed_externally: could not fetch positions: {e}")
            return

        live_pairs = {p.symbol for p in positions}
        stale = [t for pair, t in list(self._cache.items()) if pair not in live_pairs]

        if not stale:
            logger.info("Startup reconcile: all tracked trades have live positions.")
            return

        for t in stale:
            logger.info(
                f"[{t.trade_id}] {t.pair} has no live position on exchange — "
                f"marking closed (closed externally while bot was offline)"
            )
            # Attempt to get exit price + PnL from order history
            try:
                start_ms = int(t.opened_at.timestamp() * 1000)
                hist_orders = await self._client.get_history_orders(
                    symbol=t.pair, limit=10, start_time=start_ms,
                )
                close_orders = [
                    o for o in hist_orders
                    if str(o.get("tradeSide", "")).upper() == "CLOSE"
                    and str(o.get("status", "")).rstrip("_").upper() in ("FILLED", "PART_FILLED")
                ]
                if close_orders:
                    # Sort newest-first — API ordering not guaranteed
                    close_orders.sort(
                        key=lambda o: int(o.get("ctime", o.get("mtime", 0)) or 0),
                        reverse=True,
                    )
                    last = close_orders[0]  # most recent
                    t.exit_price   = float(last.get("price", 0) or 0)
                    fee            = float(last.get("fee", 0) or 0)
                    t.realized_pnl = float(last.get("realizedPNL", 0) or 0) - abs(fee)
            except Exception as e:
                logger.warning(f"[{t.trade_id}] Could not fetch close price for {t.pair}: {e}")
            t.status = TradeStatus.CLOSED
            t.closed_at = datetime.utcnow()
            await self._db.update_trade(t)
            self._cache.pop(t.pair, None)
            await self._jnl.log_trade_closed(t, "closed_externally")

        logger.info(f"Startup reconcile: marked {len(stale)} trade(s) closed (no live position).")

    # ── Sync from exchange ────────────────────────────────────────────────────

    async def sync_from_exchange(self) -> list[TradeRecord]:
        """
        Scan all live positions. For each one not already tracked, reconstruct
        trade data from TPSL + pending limit orders, then save to DB.

        Handles trades placed in the app, web UI, or other bots.
        Tolerates partial TP/SL setups (e.g. only SL set, no TPs).
        """
        imported = []

        positions = await self._client.get_positions()
        logger.info(f"sync: {len(positions)} position(s) on exchange, {len(self._cache)} tracked")

        # ── Reconcile: mark tracked trades closed if position no longer exists ──
        live_pairs = {p.symbol for p in positions}
        stale = [t for pair, t in list(self._cache.items()) if pair not in live_pairs]
        for t in stale:
            logger.info(
                f"[{t.trade_id}] {t.pair} no longer on exchange — marking closed "
                f"(likely closed via app, TP hit, or SL hit)"
            )
            # Fetch exit price + PnL from order history (same as _reconcile_closed_externally)
            try:
                start_ms = int(t.opened_at.timestamp() * 1000)
                hist_orders = await self._client.get_history_orders(
                    symbol=t.pair, limit=10, start_time=start_ms,
                )
                close_orders = [
                    o for o in hist_orders
                    if str(o.get("tradeSide", "")).upper() == "CLOSE"
                    and str(o.get("status", "")).rstrip("_").upper() in ("FILLED", "PART_FILLED")
                ]
                if close_orders:
                    close_orders.sort(
                        key=lambda o: int(o.get("ctime", o.get("mtime", 0)) or 0),
                        reverse=True,
                    )
                    last = close_orders[0]
                    t.exit_price   = float(last.get("price", 0) or 0)
                    fee            = float(last.get("fee", 0) or 0)
                    t.realized_pnl = float(last.get("realizedPNL", 0) or 0) - abs(fee)
            except Exception as e:
                logger.warning(f"[{t.trade_id}] Could not fetch close price for {t.pair}: {e}")
            t.status = TradeStatus.CLOSED
            t.closed_at = datetime.utcnow()
            await self._db.update_trade(t)
            self._cache.pop(t.pair, None)
            await self._jnl.log_trade_closed(t, "closed_externally")

        for pos in positions:
            pair = pos.symbol
            if self.has_active_trade(pair):
                logger.debug(f"sync: {pair} already tracked, skipping")
                continue

            position_id = pos.position_id
            logger.info(f"sync: importing {pair} (positionId={position_id!r})")

            # ── 1. Fetch TPSL orders ──────────────────────────────────────────
            sl_price: float = 0.0
            tp3_price: float = 0.0
            tpsl_id: str = ""

            try:
                # Fetch without symbol filter first — some exchange implementations
                # ignore the symbol param on this endpoint
                all_tpsl = await self._client.get_pending_tpsl()
                logger.debug(f"sync {pair}: {len(all_tpsl)} total TPSL order(s) on exchange")

                for o in all_tpsl:
                    o_sym = o.get("symbol", "")
                    o_pos = str(o.get("positionId", ""))

                    # Match by symbol; if positionId is available prefer exact match
                    if o_sym != pair:
                        continue
                    if position_id and o_pos and o_pos != position_id:
                        continue

                    # Bitunix returns price as string; "0" means not set
                    sl_price  = _tpsl_price(o, "slPrice")
                    tp3_price = _tpsl_price(o, "tpPrice")
                    tpsl_id   = str(o.get("id", ""))
                    logger.info(f"sync {pair}: TPSL — sl={sl_price} tp={tp3_price} id={tpsl_id!r}")
                    break

            except APIError as e:
                logger.warning(f"sync {pair}: TPSL fetch failed ({e}) — continuing without SL/TP3")

            # ── 2. Fetch pending limit orders for TP1/TP2 ─────────────────────
            tp1_price: float = 0.0
            tp2_price: float = 0.0
            tp1_order_id: str = ""
            tp2_order_id: str = ""

            try:
                all_orders = await self._client.get_pending_orders_raw(pair)
                logger.debug(f"sync {pair}: {len(all_orders)} pending limit order(s)")

                is_long = _is_long(pos.side)
                close_side = "SELL" if is_long else "BUY"

                tp_candidates = []
                for o in all_orders:
                    o_side = str(o.get("side", "")).upper()
                    o_type = str(o.get("type", o.get("orderType", ""))).upper()  # pending orders uses "type"
                    o_trade_side = str(o.get("tradeSide", "")).upper()
                    o_reduce = o.get("reduceOnly", False)
                    o_price = _f(o.get("price", 0))

                    # A TP order closes the position: correct side + LIMIT
                    # App may use tradeSide="CLOSE" instead of reduceOnly=True
                    is_closing = o_reduce or (o_trade_side == "CLOSE")
                    if o_side == close_side and o_type == "LIMIT" and is_closing and o_price > 0:
                        tp_candidates.append(o)

                # Sort by price, closest to current entry first.
                # LONG: TPs are above entry, closest = lowest TP price → ascending
                # SHORT: TPs are below entry, closest = highest TP price → descending
                tp_candidates.sort(
                    key=lambda o: _f(o.get("price", 0)),
                    reverse=not is_long,
                )

                logger.info(f"sync {pair}: {len(tp_candidates)} TP candidate order(s)")

                # Store up to 3 TP levels; any extras are logged but not tracked
                if tp_candidates:
                    tp1_price    = _f(tp_candidates[0].get("price", 0))
                    tp1_order_id = str(tp_candidates[0].get("orderId", ""))
                if len(tp_candidates) >= 2:
                    tp2_price    = _f(tp_candidates[1].get("price", 0))
                    tp2_order_id = str(tp_candidates[1].get("orderId", ""))
                if len(tp_candidates) >= 3:
                    # Override tp3 from TPSL with an actual limit order if one exists
                    limit_tp3 = _f(tp_candidates[2].get("price", 0))
                    if limit_tp3 > 0:
                        tp3_price = limit_tp3
                        logger.info(f"sync {pair}: limit order TP3 overrides TPSL tpPrice → {tp3_price}")
                if len(tp_candidates) > 3:
                    extras = [_f(o.get('price', 0)) for o in tp_candidates[3:]]
                    logger.info(f"sync {pair}: {len(extras)} extra TP order(s) beyond TP3 (not stored): {extras}")

            except APIError as e:
                logger.warning(f"sync {pair}: limit order fetch failed ({e}) — no TP1/TP2")

            # ── 3. Build and save trade record ────────────────────────────────
            trade_id = generate_trade_id()
            side = Side.LONG if _is_long(pos.side) else Side.SHORT

            trade = TradeRecord(
                trade_id=trade_id,
                pair=pair,
                side=side,
                entry=pos.entry_price,
                sl=sl_price,
                tp1=tp1_price,
                tp2=tp2_price,
                tp3=tp3_price,
                position_size=pos.size,
                leverage=pos.leverage,
                risk_amount=0.0,
                balance_at_entry=0.0,
                sl_order_id=tpsl_id,
                tp1_order_id=tp1_order_id,
                tp2_order_id=tp2_order_id,
                tp3_order_id="",    # TP3 limit order placed by bot; unknown when synced from exchange
                position_id=position_id,
            )

            try:
                await self._db.insert_trade(trade)
                self._cache[pair] = trade
                imported.append(trade)
                logger.info(
                    f"[{trade_id}] Synced {pair}: entry={pos.entry_price} "
                    f"sl={sl_price} tp1={tp1_price} tp2={tp2_price} tp3={tp3_price}"
                )
            except Exception as e:
                logger.error(f"sync {pair}: DB insert failed — {e}")

        return imported

    # ── Close trade ───────────────────────────────────────────────────────────

    async def drop_trade(self, pair: str) -> Optional["TradeRecord"]:
        """
        Remove a trade from local tracking and mark it closed in the DB,
        WITHOUT touching the exchange (no order cancels, no position close).
        Used by /resync to force a clean re-import of a pair's data.
        """
        trade = self._cache.get(pair) or await self._db.get_trade_by_pair(pair)
        if not trade:
            return None
        trade.status = TradeStatus.DROPPED
        trade.closed_at = datetime.utcnow()
        await self._db.update_trade(trade)
        self._cache.pop(pair, None)
        logger.info(f"[{trade.trade_id}] Dropped {pair} from tracking (resync requested)")
        return trade

    async def classify_orders(self, pair: str, entry_price: float, is_long: bool) -> dict:
        """
        Fetch all pending limit orders for a pair and classify them as:
          tps  — list of {price, qty, remaining, order_id}, sorted closest-first
          dcas — list of {price, qty, remaining, order_id}, sorted closest-first
          sl   — {price, qty, order_id} or None  (limit stop below/above entry)
          raw  — full list of unclassified orders (for debugging)

        Logic (works for both LONG and SHORT):
          - tradeSide=CLOSE or reduceOnly=True + price > entry (LONG)
            or price < entry (SHORT)  → TP
          - tradeSide=CLOSE or reduceOnly=True + price in wrong direction
            → limit SL (stop below entry for LONG, above for SHORT)
          - tradeSide=OPEN + same side as position + price in favourable direction
            → DCA (LONG: price < entry, SHORT: price > entry)
        """
        try:
            all_orders = await self._client.get_pending_orders_raw(pair)
        except Exception as e:
            logger.warning(f"classify_orders({pair}): fetch failed: {e}")
            return {"tps": [], "dcas": [], "sl": None, "raw": []}

        logger.info(
            f"classify_orders {pair}: is_long={is_long}, entry={entry_price}, "
            f"{len(all_orders)} raw order(s)"
        )
        for o in all_orders:
            logger.debug(
                f"  order {o.get('orderId','')} side={o.get('side','')} "
                f"tradeSide={o.get('tradeSide','')} type={o.get('type', o.get('orderType',''))} "
                f"price={o.get('price','')} qty={o.get('qty','')} "
                f"reduceOnly={o.get('reduceOnly','')}"
            )

        # Normalise is_long from position side — handles both LONG/SHORT and BUY/SELL
        open_side  = "BUY"  if is_long else "SELL"
        close_side = "SELL" if is_long else "BUY"

        tps:  list[dict] = []
        dcas: list[dict] = []
        sl_order = None

        for o in all_orders:
            o_side       = str(o.get("side",      "")).upper()
            o_trade_side = str(o.get("tradeSide", "")).upper()
            o_reduce     = bool(o.get("reduceOnly", False))
            o_price      = _f(o.get("price", 0))
            o_qty        = _f(o.get("qty",   0))
            o_filled     = _f(o.get("tradeQty", 0))
            o_remaining  = round(o_qty - o_filled, 8)
            o_id         = str(o.get("orderId", ""))

            if o_price <= 0:
                continue

            # Bitunix often omits tradeSide — derive intent from side + reduceOnly + price
            # reduceOnly=True  → closing order (TP or limit SL)
            # reduceOnly=False + tradeSide=OPEN or tradeSide='' → opening order (DCA)
            # tradeSide=CLOSE without reduceOnly also means closing
            is_closing = o_reduce or (o_trade_side == "CLOSE")
            is_opening = not is_closing  # anything not closing is an opening order

            entry = entry_price  # alias for readability
            order_info = {"price": o_price, "qty": o_qty, "remaining": o_remaining, "order_id": o_id}

            if is_closing and o_side == close_side:
                # Closing order on the correct side — TP or limit SL based on price direction
                if is_long and o_price > entry:
                    tps.append(order_info)
                elif not is_long and o_price < entry:
                    tps.append(order_info)
                elif is_long and o_price < entry:
                    # Below entry on a LONG close = limit stop loss
                    if sl_order is None or o_price > sl_order["price"]:
                        sl_order = order_info
                elif not is_long and o_price > entry:
                    # Above entry on a SHORT close = limit stop loss
                    if sl_order is None or o_price < sl_order["price"]:
                        sl_order = order_info

            elif is_opening and o_side == open_side:
                # Opening order in same direction as position → DCA (must be favourable price)
                if is_long and o_price < entry:
                    dcas.append(order_info)
                elif not is_long and o_price > entry:
                    dcas.append(order_info)

        # Sort TPs: LONG ascending (closest = lowest), SHORT descending (closest = highest)
        tps.sort(key=lambda x: x["price"], reverse=not is_long)
        # Sort DCAs: LONG descending (closest = highest below entry), SHORT ascending (closest = lowest above entry)
        dcas.sort(key=lambda x: x["price"], reverse=is_long)

        logger.info(
            f"classify_orders {pair}: {len(tps)} TP(s), {len(dcas)} DCA(s), "
            f"sl={'yes' if sl_order else 'no'}"
        )
        return {"tps": tps, "dcas": dcas, "sl": sl_order, "raw": all_orders}

    async def close_trade(self, pair: str, reason: str = "manual") -> Optional[TradeRecord]:
        trade = self._cache.get(pair) or await self._db.get_trade_by_pair(pair)
        if not trade:
            return None

        try:
            await self._client.cancel_all_orders(pair)
        except APIError as e:
            logger.warning(f"cancel_all for {pair} failed: {e}")

        position = await self._client.get_position(pair)
        close_price = 0.0
        if position and position.size > 0:
            close_price = position.entry_price  # best known price before close
            try:
                if not position.position_id:
                    raise APIError("No positionId available for flash close")
                await self._client.flash_close_position(position.position_id, symbol=pair, side=position.side)
            except APIError:
                # Fallback: manual market close with positionId for hedge mode
                await self._client.close_position(
                    pair, position.side, position.size,
                    position_id=position.position_id or None,
                )
            # Get actual fill price from ticker after close
            try:
                close_price = await self._client.get_ticker_price(pair)
            except Exception:
                pass  # keep position entry_price as approximation

        # Calculate approximate PnL from close price
        if close_price and trade.position_size and trade.entry:
            if trade.side == Side.LONG:
                pnl = (close_price - trade.entry) * trade.position_size
            else:
                pnl = (trade.entry - close_price) * trade.position_size
            trade.realized_pnl = round(pnl, 4)
            trade.exit_price   = close_price

        trade.status = TradeStatus.CLOSED
        trade.closed_at = datetime.utcnow()
        await self._db.update_trade(trade)
        self._cache.pop(pair, None)
        await self._jnl.log_trade_closed(trade, reason)
        logger.info(f"[{trade.trade_id}] Closed {pair} reason={reason}")
        return trade

    async def close_all(self) -> list[TradeRecord]:
        pairs = list(self._cache.keys())
        closed = []
        for pair in pairs:
            try:
                t = await self.close_trade(pair, reason="closeall")
                if t:
                    closed.append(t)
            except Exception as e:
                logger.error(f"Failed to close {pair}: {e}")
        await self._jnl.log_closeall(closed)
        return closed

    async def cancel_pair(self, pair: str) -> None:
        await self._client.cancel_all_orders(pair)
        logger.info(f"Cancelled all orders for {pair}")

    # ── Modify SL ─────────────────────────────────────────────────────────────

    async def modify_sl(self, pair: str, new_sl: float) -> float:
        """Move SL to new_sl on exchange and in DB. Returns old SL price."""
        trade = self._cache.get(pair)
        if not trade:
            raise ValueError(
                f"No active trade tracked for {pair}.\n"
                f"Run /sync first if this position was placed in the app."
            )

        old_sl = trade.sl

        if trade.position_id:
            await self._client.modify_position_tpsl(
                symbol=pair, position_id=trade.position_id, sl_price=new_sl,
            )
        else:
            # Fallback: cancel old SL limit, place new one
            if trade.sl_order_id:
                try:
                    await self._client.cancel_order(pair, trade.sl_order_id)
                except APIError:
                    pass
            sl_side = "SELL" if trade.side == Side.LONG else "BUY"
            new_id = await self._client.place_order(
                symbol=pair, side=sl_side, order_type="LIMIT",
                qty=trade.position_size, price=new_sl, reduce_only=True, trade_side="CLOSE",
            )
            trade.sl_order_id = new_id

        trade.sl = new_sl
        await self._db.update_trade(trade)
        logger.info(f"[{trade.trade_id}] SL updated {old_sl} → {new_sl} for {pair}")
        return old_sl

    # ── Soft SL management ────────────────────────────────────────────────────

    async def set_soft_sl(self, pair: str, price: float, timeframe: str) -> None:
        """
        Set or update the soft SL for a tracked trade.
        No exchange order is placed — the SoftSLMonitor watches candle closes.
        """
        trade = self._cache.get(pair)
        if not trade:
            raise ValueError(
                f"No active trade tracked for {pair}. "
                f"Run /sync first if placed in the app."
            )
        trade.soft_sl_price     = price
        trade.soft_sl_timeframe = timeframe
        await self._db.update_trade(trade)
        logger.info(f"[{trade.trade_id}] Soft SL set: {pair} {price} {timeframe}")

    async def clear_soft_sl(self, pair: str) -> None:
        """Remove the soft SL from a trade without touching exchange orders."""
        trade = self._cache.get(pair)
        if not trade:
            return
        trade.soft_sl_price     = None
        trade.soft_sl_timeframe = None
        await self._db.update_trade(trade)
        logger.info(f"[{trade.trade_id}] Soft SL cleared for {pair}")
    # ── Entry fill poller ─────────────────────────────────────────────────────

    ENTRY_POLL_INTERVAL    = 30  # seconds
    TP_POLL_INTERVAL       = 20  # seconds — how often to check if TP1/2/3 have filled
    POSITION_POLL_INTERVAL = 15  # seconds — how often to check if position was closed by SL/exchange

    async def run_entry_fill_poller(self, notify_callback) -> None:
        """Poll unfilled limit entries every 30s; place TPs when filled."""
        logger.info("Entry fill poller started.")
        while True:
            try:
                await asyncio.sleep(self.ENTRY_POLL_INTERVAL)
                await self._check_pending_entries(notify_callback)
            except asyncio.CancelledError:
                logger.info("Entry fill poller cancelled.")
                break
            except Exception as e:
                logger.error(f"Entry fill poller error: {e}", exc_info=True)

    async def _check_pending_entries(self, notify_callback) -> None:
        s = get_settings()
        pending = [t for t in self._cache.values() if not t.position_id and t.entry_order_id]
        # Also check DCA fills for trades that already have a position
        dca_pending = [t for t in self._cache.values()
                       if t.position_id and t.dca_order_id]
        if not pending:
            return
        logger.debug(f"Entry fill poller: {len(pending)} pending entrie(s)")
        for trade in pending:
            pair = trade.pair
            try:
                order  = await self._client.get_order_status(trade.entry_order_id)
                status = order.get("status", "UNKNOWN")
                logger.debug(f"[{trade.trade_id}] {pair} entry order status: {status}")
                if status not in ("FILLED", "PART_FILLED"):
                    continue
                await asyncio.sleep(0.5)
                position = await self._client.get_position(pair)
                if not position or not position.position_id:
                    logger.warning(f"[{trade.trade_id}] {pair} filled but no position yet — retrying")
                    continue
                position_id       = position.position_id
                trade.position_id = position_id
                actual_qty        = position.size
                close_side        = "SELL" if trade.side == Side.LONG else "BUY"
                sym_info          = await self._client.get_symbol_info(pair)
                qp                = sym_info["basePrecision"]
                tp1_qty, tp2_qty, tp3_qty = _safe_tp_qtys(
                    actual_qty, s.tp1_pct, s.tp2_pct, qp, sym_info["minTradeVolume"]
                )
                if trade.tp1 and trade.tp1 > 0:
                    trade.tp1_order_id = await self._client.place_order(
                        symbol=pair, side=close_side, order_type="LIMIT",
                        qty=tp1_qty, price=trade.tp1, reduce_only=True,
                        trade_side="CLOSE", position_id=position_id,
                    )
                if tp2_qty > 0 and trade.tp2 and trade.tp2 > 0:
                    trade.tp2_order_id = await self._client.place_order(
                        symbol=pair, side=close_side, order_type="LIMIT",
                        qty=tp2_qty, price=trade.tp2, reduce_only=True,
                        trade_side="CLOSE", position_id=position_id,
                    )
                if tp3_qty > 0 and trade.tp3 and trade.tp3 > 0:
                    trade.tp3_order_id = await self._client.place_order(
                        symbol=pair, side=close_side, order_type="LIMIT",
                        qty=tp3_qty, price=trade.tp3, reduce_only=True,
                        trade_side="CLOSE", position_id=position_id,
                    )
                if trade.sl and trade.sl > 0:
                    trade.sl_order_id = await self._client.place_tpsl(
                        symbol=pair, position_id=position_id, sl_price=trade.sl,
                    )
                await self._db.update_trade(trade)
                logger.info(
                    f"[{trade.trade_id}] {pair} entry filled — "
                    f"tp1={trade.tp1_order_id} tp2={trade.tp2_order_id} tp3={trade.tp3_order_id}"
                )
                from messages import _fmt
                side_lbl = "🟢 LONG" if trade.side == Side.LONG else "🔴 SHORT"
                sep = "─" * 28
                lines = [
                    "<b>ENTRY FILLED</b>  ✅",
                    f"<code>{sep}</code>",
                    "",
                    f"  <b>{pair}</b>  {side_lbl}  ·  {actual_qty} @ ${_fmt(position.entry_price)}",
                    "",
                    f"  <code>TP1</code>  ${_fmt(trade.tp1)}",
                    f"  <code>TP2</code>  ${_fmt(trade.tp2)}",
                    f"  <code>TP3</code>  ${_fmt(trade.tp3)}",
                    "",
                    "  <i>All TP orders placed as limit orders.</i>",
                ]
                await notify_callback("\n".join(lines))
            except APIError as e:
                logger.error(f"[{trade.trade_id}] {pair} entry poll APIError: {e}")
            except Exception as e:
                logger.error(f"[{trade.trade_id}] {pair} entry poll error: {e}", exc_info=True)

        # ── DCA fill detection ──────────────────────────────────────────────────
        for trade in dca_pending:
            pair = trade.pair
            try:
                order = await self._client.get_order_status(trade.dca_order_id)
                status = order.get("status", "UNKNOWN")
                if status not in ("FILLED", "PART_FILLED"):
                    continue
                # DCA has filled — get current live position size
                position = await self._client.get_position(pair)
                if not position:
                    continue
                new_total_qty = position.size
                close_side    = "SELL" if trade.side == Side.LONG else "BUY"
                sym_info      = await self._client.get_symbol_info(pair)
                qp            = sym_info["basePrecision"]
                min_qty       = sym_info["minTradeVolume"]

                # Cancel old TP orders and re-place with new total qty
                for oid_attr in ("tp1_order_id", "tp2_order_id", "tp3_order_id"):
                    oid = getattr(trade, oid_attr, "")
                    if oid:
                        try:
                            await self._client.cancel_order(pair, oid)
                        except APIError:
                            pass
                        setattr(trade, oid_attr, "")

                tp1_qty, tp2_qty, tp3_qty = _safe_tp_qtys(
                    new_total_qty, s.tp1_pct, s.tp2_pct, qp, min_qty
                )
                if trade.tp1 and trade.tp1 > 0:
                    trade.tp1_order_id = await self._client.place_order(
                        symbol=pair, side=close_side, order_type="LIMIT",
                        qty=tp1_qty, price=trade.tp1, reduce_only=True,
                        trade_side="CLOSE", position_id=trade.position_id,
                    )
                if tp2_qty > 0 and trade.tp2 and trade.tp2 > 0:
                    trade.tp2_order_id = await self._client.place_order(
                        symbol=pair, side=close_side, order_type="LIMIT",
                        qty=tp2_qty, price=trade.tp2, reduce_only=True,
                        trade_side="CLOSE", position_id=trade.position_id,
                    )
                if tp3_qty > 0 and trade.tp3 and trade.tp3 > 0:
                    trade.tp3_order_id = await self._client.place_order(
                        symbol=pair, side=close_side, order_type="LIMIT",
                        qty=tp3_qty, price=trade.tp3, reduce_only=True,
                        trade_side="CLOSE", position_id=trade.position_id,
                    )
                trade.position_size = new_total_qty
                trade.dca_order_id  = ""   # mark DCA as processed
                await self._db.update_trade(trade)
                logger.info(
                    f"[{trade.trade_id}] DCA filled for {pair} — "
                    f"new total qty={new_total_qty} TP orders re-placed"
                )
                await notify_callback(
                    f"<b>DCA FILLED</b>  ✅\n"
                    f"<code>{'─'*28}</code>\n\n"
                    f"  <b>{pair}</b> DCA filled — position expanded\n"
                    f"  <code>New total qty  </code>  {new_total_qty}\n"
                    f"  <i>TP orders re-placed for full position size.</i>"
                )
            except APIError as e:
                logger.warning(f"[{trade.trade_id}] {pair} DCA poll APIError: {e}")
            except Exception as e:
                logger.error(f"[{trade.trade_id}] {pair} DCA poll error: {e}", exc_info=True)

    # ── TP fill poller ────────────────────────────────────────────────────────

    async def run_tp_fill_poller(self, notify_callback) -> None:
        """Poll active trades every 20s to detect TP1/TP2/TP3 fills."""
        logger.info("TP fill poller started.")
        while True:
            try:
                await asyncio.sleep(self.TP_POLL_INTERVAL)
                await self._check_tp_fills(notify_callback)
            except asyncio.CancelledError:
                logger.info("TP fill poller cancelled.")
                break
            except Exception as e:
                logger.error(f"TP fill poller error: {e}", exc_info=True)

    async def _persist_seen_close_id(self, trade: "TradeRecord", order_id: str) -> None:
        """Add order_id to trade.seen_close_ids and persist to DB."""
        existing = set(s for s in (trade.seen_close_ids or "").split(",") if s)
        existing.add(order_id)
        trade.seen_close_ids = ",".join(existing)
        try:
            await self._db.update_seen_close_ids(trade.trade_id, trade.seen_close_ids)
        except Exception as e:
            logger.warning(f"[{trade.trade_id}] Failed to persist seen_close_id {order_id}: {e}")

    async def _check_tp_fills(self, notify_callback) -> None:
        """
        For each active trade, check if TP1/TP2/TP3 orders have filled.
        On TP1 fill: optionally move SL to breakeven.
        Fires Discord webhooks for each fill.
        """
        from models import Side
        s = get_settings()

        for trade in list(self._cache.values()):
            pair = trade.pair
            try:
                for tp_num, order_id_attr, tp_price_attr in [
                    (1, "tp1_order_id", "tp1"),
                    (2, "tp2_order_id", "tp2"),
                    (3, "tp3_order_id", "tp3"),
                ]:
                    order_id = getattr(trade, order_id_attr, None)
                    tp_price = getattr(trade, tp_price_attr, 0)
                    if not order_id or not tp_price:
                        continue
                    # Skip if already recorded as filled (clear order_id after fill)
                    order = await self._client.get_order_status(order_id)
                    status = order.get("status", "UNKNOWN")
                    if status not in ("FILLED", "PART_FILLED"):
                        continue

                    filled_qty = float(order.get("tradeQty", 0) or order.get("qty", 0))
                    # Fetch live position size for accurate remaining — trade.position_size
                    # is never updated after partial fills
                    try:
                        live = await self._client.get_position(pair)
                        remaining = live.size if live else 0.0
                    except Exception:
                        remaining = max(0.0, trade.position_size - filled_qty)

                    logger.info(
                        f"[{trade.trade_id}] TP{tp_num} FILLED — {pair} "
                        f"@ ${tp_price} qty={filled_qty} remaining={remaining}"
                    )

                    # Fire Discord webhook
                    await self._jnl.log_tp_hit(
                        trade=trade,
                        tp_num=tp_num,
                        tp_price=tp_price,
                        qty_closed=filled_qty,
                        remaining=remaining,
                    )

                    # Notify Telegram
                    if s.notify_tp_hits:
                        from messages import _fmt
                        side_lbl = "🟢 LONG" if trade.side == Side.LONG else "🔴 SHORT"
                        await notify_callback(
                            f"<b>TP{tp_num} FILLED</b>  ✅\n"
                            f"<code>{'─'*28}</code>\n\n"
                            f"  <b>{pair}</b>  {side_lbl}\n"
                            f"  <code>Fill price   </code>  ${_fmt(tp_price)}\n"
                            f"  <code>Qty closed   </code>  {filled_qty}\n"
                            f"  <code>Remaining    </code>  {remaining}"
                        )

                    # Clear the order_id and add to seen set so close poller
                    # won't re-classify it as an unknown close
                    if order_id:
                        self._seen_close_orders.add(order_id)
                        await self._persist_seen_close_id(trade, order_id)
                    setattr(trade, order_id_attr, "")
                    await self._db.update_trade(trade)

                    # Auto SL → BE after TP1
                    if tp_num == 1 and s.auto_move_sl_to_be and trade.sl != trade.entry:
                        old_sl = trade.sl
                        await self.modify_sl(pair, trade.entry)
                        # Cancel candle monitoring — no longer needed at breakeven
                        await self.clear_soft_sl(pair)
                        await self._jnl.log_sl_moved_to_be(trade, old_sl)
                        await notify_callback(
                            f"<b>SL → BREAKEVEN</b>  🔒\n"
                            f"<code>{'─'*28}</code>\n\n"
                            f"  <b>{pair}</b>  SL moved to entry\n"
                            f"  <code>Old SL  </code>  ${_fmt(old_sl)}\n"
                            f"  <code>New SL  </code>  ${_fmt(trade.entry)}  <i>(breakeven)</i>"
                        )

            except APIError as e:
                logger.warning(f"[{trade.trade_id}] {pair} TP poll APIError: {e}")
            except Exception as e:
                logger.error(f"[{trade.trade_id}] {pair} TP poll error: {e}", exc_info=True)

    # ── Position close poller (SL hit / external close detection) ─────────────

    async def run_position_close_poller(self, notify_callback) -> None:
        """
        Poll every 15s to detect positions closed by the exchange:
        SL hit, TP3 hit via TPSL order, manual close in app, or liquidation.
        Fetches history to get actual close price + PnL, then classifies the close.
        """
        logger.info("Position close poller started.")
        while True:
            try:
                await asyncio.sleep(self.POSITION_POLL_INTERVAL)
                await self._check_position_closes(notify_callback)
            except asyncio.CancelledError:
                logger.info("Position close poller cancelled.")
                break
            except Exception as e:
                logger.error(f"Position close poller error: {e}", exc_info=True)

    async def _check_position_closes(self, notify_callback) -> None:
        """
        For each tracked trade, fetch recent filled CLOSE orders via get_history_orders.
        Any filled CLOSE order we haven't seen before = position was closed.
        Classify by comparing fill price to TP/SL levels.
        Also handles full position close (SL hit, manual, liquidation).
        """
        import time
        from models import Side
        from messages import _fmt

        for trade in list(self._cache.values()):
            if not trade.position_id:
                continue  # entry not filled yet, skip

            pair = trade.pair
            try:
                # Only look at orders since this trade opened (avoid false positives)
                start_ms = int(trade.opened_at.timestamp() * 1000)

                orders = await self._client.get_history_orders(
                    symbol=pair, limit=50, start_time=start_ms,
                )

                # Filter to FILLED CLOSE orders only (client-side — API has no status filter)
                close_orders = [
                    o for o in orders
                    if str(o.get("tradeSide", "")).upper() == "CLOSE"
                    and str(o.get("status", "")).rstrip("_").upper() in ("FILLED", "PART_FILLED")
                ]

                if not close_orders:
                    continue

                # Collect order IDs we already know about (TP1/2/3 placed by bot)
                known_ids = {
                    oid for oid in [
                        trade.tp1_order_id,
                        trade.tp2_order_id,
                        trade.tp3_order_id,
                    ] if oid
                }

                for order in close_orders:
                    order_id   = str(order.get("orderId", ""))
                    fill_price = float(order.get("price", 0) or 0)
                    fill_qty   = float(order.get("tradeQty", 0) or order.get("qty", 0))
                    raw_pnl    = float(order.get("realizedPNL", 0) or 0)
                    fee        = float(order.get("fee", 0) or 0)

                    # Skip TP1/2/3 orders already handled by TP fill poller
                    if order_id in known_ids:
                        continue

                    # Skip if already processed by a previous poll cycle
                    if order_id in self._seen_close_orders:
                        continue

                    # Classify what caused this close
                    reason = _classify_close(trade, fill_price, raw_pnl)

                    logger.info(
                        f"[{trade.trade_id}] {pair} close order detected — "
                        f"orderId={order_id} price={fill_price} pnl={raw_pnl:.2f} reason={reason}"
                    )

                    net_pnl = raw_pnl - abs(fee)

                    # Check if position is now fully gone
                    live_pos = await self._client.get_position(pair)
                    position_fully_closed = (live_pos is None or live_pos.size == 0)

                    if position_fully_closed:
                        # Full close — update DB and remove from cache
                        trade.status       = TradeStatus.CLOSED
                        trade.closed_at    = datetime.utcnow()
                        trade.exit_price   = fill_price
                        trade.realized_pnl = net_pnl
                        await self._db.update_trade(trade)
                        self._cache.pop(pair, None)

                        # Discord + Telegram
                        if reason == "sl_hit":
                            await self._jnl.log_sl_hit(
                                trade, fill_price, raw_pnl, fee=fee
                            )
                        else:
                            await self._jnl.log_position_closed_externally(
                                trade, fill_price, raw_pnl, reason, fee=fee
                            )

                        side_lbl = "🟢 LONG" if trade.side == Side.LONG else "🔴 SHORT"
                        icons  = {"sl_hit": "🔴", "tp3_hit": "🥉", "manual_close": "🔒",
                                  "liquidated": "💀", "unknown": "📭"}
                        labels = {"sl_hit": "STOP LOSS HIT", "tp3_hit": "TP3 HIT",
                                  "manual_close": "CLOSED MANUALLY", "liquidated": "LIQUIDATED",
                                  "unknown": "POSITION CLOSED"}
                        icon  = icons.get(reason, "📭")
                        label = labels.get(reason, "POSITION CLOSED")
                        sign  = "+" if net_pnl >= 0 else ""
                        _should_notify = (
                            s.notify_sl_hits if reason == "sl_hit"
                            else True  # always notify for tp3, manual, liquidated, unknown
                        )
                        if _should_notify:
                            await notify_callback(
                                f"<b>{icon} {label}</b>\n"
                                f"<code>{'─'*28}</code>\n\n"
                                f"  <b>{pair}</b>  {side_lbl}\n"
                                f"  <code>Entry        </code>  ${_fmt(trade.entry)}\n"
                                f"  <code>Close        </code>  ${_fmt(fill_price)}\n"
                                f"  <code>Net PnL      </code>  {sign}${net_pnl:.2f}"
                            )
                        break  # position is gone, no point processing more orders

                    else:
                        # Partial close (e.g. TP hit but position still open)
                        # Persist to DB so this order isn't re-detected on restart
                        self._seen_close_orders.add(order_id)
                        await self._persist_seen_close_id(trade, order_id)
                        logger.info(
                            f"[{trade.trade_id}] {pair} partial close detected "
                            f"@ {fill_price} qty={fill_qty} — position still open"
                        )

            except APIError as e:
                logger.warning(f"[{trade.trade_id}] {pair} close poll APIError: {e}")
            except Exception as e:
                logger.error(f"[{trade.trade_id}] {pair} close poll error: {e}", exc_info=True)




    # ── /tp — add take-profit order ───────────────────────────────────────────

    async def add_tp_order(
        self,
        pair: str,
        price: float,      # 0.0 = market (CMP)
        qty: float,
        tp_slot: int,      # 1, 2, or 3 — which slot to fill
    ) -> str:
        """
        Place a single TP limit (or market) order for qty tokens on pair.
        Updates trade.tp{n}, trade.tp{n}_order_id and persists to DB.
        Returns the new order_id.
        """
        trade = self._cache.get(pair)
        if not trade:
            raise ValueError(
                f"No active trade tracked for {pair}.\n"
                f"Run /sync first if this position was placed in the app."
            )
        if not trade.position_id:
            raise ValueError(
                f"Trade for {pair} has no positionId yet — wait for entry fill."
            )

        close_side = "SELL" if trade.side == Side.LONG else "BUY"
        is_market  = price == 0.0

        order_id = await self._client.place_order(
            symbol=pair,
            side=close_side,
            order_type="MARKET" if is_market else "LIMIT",
            qty=qty,
            price=None if is_market else price,
            reduce_only=True,
            trade_side="CLOSE",
            position_id=trade.position_id,
        )

        # Update trade record with new TP price and order id
        if tp_slot == 1:
            trade.tp1 = price
            trade.tp1_order_id = order_id
        elif tp_slot == 2:
            trade.tp2 = price
            trade.tp2_order_id = order_id
        else:
            trade.tp3 = price
            trade.tp3_order_id = order_id

        await self._db.update_trade(trade)
        logger.info(
            f"[{trade.trade_id}] TP{tp_slot} added for {pair}: "
            f"price={'MARKET' if is_market else price}  qty={qty}  orderId={order_id}"
        )
        return order_id

    async def rebalance_tps(self, pair: str) -> dict:
        """
        Cancel all existing TP orders for a trade and re-place them
        proportionally based on the live position size from the exchange.
        Returns a dict with keys: tp1_qty, tp2_qty, tp3_qty, total_qty.
        Raises ValueError if no active trade or no position found.
        """
        trade = self.get_active_trade(pair)
        if not trade:
            raise ValueError(f"No active trade for {pair}")

        s           = get_settings()
        sym_info    = await self._client.get_symbol_info(pair)
        qp          = sym_info["basePrecision"]
        min_qty     = sym_info["minTradeVolume"]

        # Fetch live position size from exchange
        position = await self._client.get_position(pair)
        if not position:
            raise ValueError(f"No live position found for {pair} on exchange")
        live_qty   = position.size
        close_side = "SELL" if trade.side == Side.LONG else "BUY"

        # Cancel ALL open orders for the pair (catches ghost orders not tracked in DB)
        try:
            await self._client.cancel_all_orders(pair)
        except APIError as e:
            logger.warning(f"rebalance_tps: cancel_all_orders {pair} failed: {e}")
        for attr in ("tp1_order_id", "tp2_order_id", "tp3_order_id"):
            setattr(trade, attr, "")

        # Re-place with corrected proportions
        tp1_qty, tp2_qty, tp3_qty = _safe_tp_qtys(live_qty, s.tp1_pct, s.tp2_pct, qp, min_qty)

        if trade.tp1 and trade.tp1 > 0:
            trade.tp1_order_id = await self._client.place_order(
                symbol=pair, side=close_side, order_type="LIMIT",
                qty=tp1_qty, price=trade.tp1, reduce_only=True,
                trade_side="CLOSE", position_id=trade.position_id,
            )
        if tp2_qty > 0 and trade.tp2 and trade.tp2 > 0:
            trade.tp2_order_id = await self._client.place_order(
                symbol=pair, side=close_side, order_type="LIMIT",
                qty=tp2_qty, price=trade.tp2, reduce_only=True,
                trade_side="CLOSE", position_id=trade.position_id,
            )
        if tp3_qty > 0 and trade.tp3 and trade.tp3 > 0:
            trade.tp3_order_id = await self._client.place_order(
                symbol=pair, side=close_side, order_type="LIMIT",
                qty=tp3_qty, price=trade.tp3, reduce_only=True,
                trade_side="CLOSE", position_id=trade.position_id,
            )

        trade.position_size = live_qty
        await self._db.update_trade(trade)
        logger.info(
            f"[{trade.trade_id}] rebalance_tps {pair}: live_qty={live_qty} "
            f"tp1={tp1_qty} tp2={tp2_qty} tp3={tp3_qty}"
        )
        return {"tp1_qty": tp1_qty, "tp2_qty": tp2_qty, "tp3_qty": tp3_qty, "total_qty": live_qty}

    # ── /dca — add DCA order with independent risk sizing ────────────────────

    async def add_dca_order(
        self,
        pair: str,
        price: float,       # DCA entry price
        dca_qty: float,     # tokens to buy — caller computes from risk/SL
        new_sl: float,      # new combined SL to move exchange order to
    ) -> str:
        """
        Place a DCA limit entry order. Moves the exchange SL to new_sl.
        Updates trade.dca, trade.dca_order_id, trade.sl and persists to DB.
        Returns the new dca order_id.
        """
        trade = self._cache.get(pair)
        if not trade:
            raise ValueError(
                f"No active trade tracked for {pair}.\n"
                f"Run /sync first if this position was placed in the app."
            )
        if trade.dca_order_id:
            raise ValueError(
                f"{pair} already has a pending DCA order. "
                f"Cancel it with /cancelpair first."
            )

        entry_side = "BUY" if trade.side == Side.LONG else "SELL"
        dca_order_id = await self._client.place_order(
            symbol=pair,
            side=entry_side,
            order_type="LIMIT",
            qty=dca_qty,
            price=price,
            trade_side="OPEN",
        )

        # Move SL to new combined level
        old_sl = trade.sl
        if trade.position_id and new_sl and new_sl > 0:
            await self._client.modify_position_tpsl(
                symbol=pair, position_id=trade.position_id, sl_price=new_sl,
            )
            trade.sl = new_sl

        trade.dca           = price
        trade.dca_order_id  = dca_order_id
        await self._db.update_trade(trade)

        logger.info(
            f"[{trade.trade_id}] DCA added for {pair}: "
            f"price={price}  qty={dca_qty}  orderId={dca_order_id}  "
            f"SL moved {old_sl} → {new_sl}"
        )
        return dca_order_id


def _classify_close(trade, close_price: float, pnl: float) -> str:
    """
    Classify why a position was closed based on fill price vs trade levels.
    Returns: 'sl_hit' | 'tp3_hit' | 'liquidated' | 'manual_close' | 'unknown'
    """
    from models import Side

    if not close_price:
        return "unknown"

    sl    = trade.sl    or 0
    tp3   = trade.tp3   or 0
    entry = trade.entry or 0
    is_long = trade.side == Side.LONG

    # Liquidation: extreme loss well beyond SL
    if pnl < 0 and sl and abs(pnl) > abs(entry - sl) * trade.position_size * 1.5:
        return "liquidated"

    # SL hit: close_price at or beyond the SL level (0.5% slippage tolerance)
    if sl:
        if is_long  and close_price <= sl * 1.005:
            return "sl_hit"
        if not is_long and close_price >= sl * 0.995:
            return "sl_hit"

    # TP3 hit: close_price at or beyond TP3
    if tp3:
        if is_long  and close_price >= tp3 * 0.995:
            return "tp3_hit"
        if not is_long and close_price <= tp3 * 1.005:
            return "tp3_hit"

    return "manual_close"