"""
SQLite database layer using aiosqlite.
Persists trade records across bot restarts.
"""
import asyncio
import logging
from datetime import datetime
from typing import Optional

import aiosqlite

from config import DB_PATH
from models import TradeRecord, TradeStatus, Side

logger = logging.getLogger(__name__)

CREATE_TRADES_TABLE = """
CREATE TABLE IF NOT EXISTS trades (
    trade_id        TEXT PRIMARY KEY,
    pair            TEXT NOT NULL,
    side            TEXT NOT NULL,
    entry           REAL NOT NULL,
    sl              REAL,
    tp1             REAL,
    tp2             REAL,
    tp3             REAL,
    position_size   REAL NOT NULL,
    leverage        INTEGER NOT NULL,
    risk_amount     REAL NOT NULL,
    balance_at_entry REAL NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open',
    dca             REAL,
    entry_order_id  TEXT,
    sl_order_id     TEXT,
    tp1_order_id    TEXT,
    tp2_order_id    TEXT,
    tp3_order_id    TEXT,
    dca_order_id    TEXT,
    position_id     TEXT,
    soft_sl_price       REAL,
    soft_sl_timeframe   TEXT,
    strategy            TEXT,
    opened_at       TEXT NOT NULL,
    closed_at       TEXT,
    realized_pnl    REAL NOT NULL DEFAULT 0.0,
    exit_price      REAL NOT NULL DEFAULT 0.0,
    seen_close_ids  TEXT NOT NULL DEFAULT ''
);
"""

MIGRATE_ADD_POSITION_ID = """
ALTER TABLE trades ADD COLUMN position_id TEXT;
"""

MIGRATE_ADD_SOFT_SL = """
ALTER TABLE trades ADD COLUMN soft_sl_price REAL;
"""

MIGRATE_ADD_SOFT_SL_TF = """
ALTER TABLE trades ADD COLUMN soft_sl_timeframe TEXT;
"""

MIGRATE_ADD_STRATEGY = """
ALTER TABLE trades ADD COLUMN strategy TEXT;
"""

MIGRATE_ADD_SEEN_CLOSE_IDS = """
ALTER TABLE trades ADD COLUMN seen_close_ids TEXT NOT NULL DEFAULT '';
"""

# SQLite cannot ALTER COLUMN to drop NOT NULL constraints.
# We recreate the trades table preserving all data, with tp1/tp2/tp3/sl nullable.
# This runs once — guarded by checking if tp1 still has a NOT NULL constraint.
MIGRATE_NULLABLE_TPS = """
CREATE TABLE IF NOT EXISTS trades_new (
    trade_id        TEXT PRIMARY KEY,
    pair            TEXT NOT NULL,
    side            TEXT NOT NULL,
    entry           REAL NOT NULL,
    sl              REAL,
    tp1             REAL,
    tp2             REAL,
    tp3             REAL,
    position_size   REAL NOT NULL,
    leverage        INTEGER NOT NULL,
    risk_amount     REAL NOT NULL,
    balance_at_entry REAL NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open',
    dca             REAL,
    entry_order_id  TEXT,
    sl_order_id     TEXT,
    tp1_order_id    TEXT,
    tp2_order_id    TEXT,
    tp3_order_id    TEXT,
    dca_order_id    TEXT,
    position_id     TEXT,
    soft_sl_price       REAL,
    soft_sl_timeframe   TEXT,
    strategy            TEXT,
    opened_at       TEXT NOT NULL,
    closed_at       TEXT,
    realized_pnl    REAL NOT NULL DEFAULT 0.0,
    exit_price      REAL NOT NULL DEFAULT 0.0,
    seen_close_ids  TEXT NOT NULL DEFAULT ''
);
"""


def _row_to_trade(row: aiosqlite.Row) -> TradeRecord:
    keys = list(row.keys())
    return TradeRecord(
        trade_id=row["trade_id"],
        pair=row["pair"],
        side=Side(row["side"]),
        entry=row["entry"],
        sl=row["sl"],
        tp1=row["tp1"],
        tp2=row["tp2"],
        tp3=row["tp3"],
        position_size=row["position_size"],
        leverage=row["leverage"],
        risk_amount=row["risk_amount"],
        balance_at_entry=row["balance_at_entry"],
        status=TradeStatus(row["status"]),
        dca=row["dca"],
        entry_order_id=row["entry_order_id"],
        sl_order_id=row["sl_order_id"],
        tp1_order_id=row["tp1_order_id"],
        tp2_order_id=row["tp2_order_id"],
        tp3_order_id=row["tp3_order_id"],
        dca_order_id=row["dca_order_id"],
        position_id=row["position_id"] if "position_id" in keys else None,
        soft_sl_price=row["soft_sl_price"] if "soft_sl_price" in keys else None,
        soft_sl_timeframe=row["soft_sl_timeframe"] if "soft_sl_timeframe" in keys else None,
        strategy=row["strategy"] if "strategy" in keys else None,
        opened_at=datetime.fromisoformat(row["opened_at"]),
        closed_at=datetime.fromisoformat(row["closed_at"]) if row["closed_at"] else None,
        realized_pnl=row["realized_pnl"],
        exit_price=row["exit_price"],
        seen_close_ids=row["seen_close_ids"] if "seen_close_ids" in keys else "",
    )


class Database:
    def __init__(self) -> None:
        self._db: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(DB_PATH)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL;")
        await self._db.execute("PRAGMA foreign_keys=ON;")
        await self._db.execute(CREATE_TRADES_TABLE)
        # Migrate existing DBs that don't have position_id column yet
        for migration, label in [
            (MIGRATE_ADD_POSITION_ID, 'position_id'),
            (MIGRATE_ADD_SOFT_SL,     'soft_sl_price'),
            (MIGRATE_ADD_SOFT_SL_TF,  'soft_sl_timeframe'),
            (MIGRATE_ADD_STRATEGY,       'strategy'),
            (MIGRATE_ADD_SEEN_CLOSE_IDS, 'seen_close_ids'),
        ]:
            try:
                await self._db.execute(migration)
                await self._db.commit()
                logger.info(f'DB migration: added {label} column')
            except Exception:
                pass  # column already exists
        # Migrate: drop NOT NULL from tp1/tp2/tp3/sl if still present.
        # SQLite can't ALTER COLUMN — we recreate the table preserving all data.
        try:
            cur = await self._db.execute("PRAGMA table_info(trades)")
            cols = await cur.fetchall()
            tp1_notnull = any(c["name"] == "tp1" and c["notnull"] == 1 for c in cols)
            if tp1_notnull:
                logger.info("DB migration: removing NOT NULL from tp1/tp2/tp3/sl ...")
                await self._db.execute(MIGRATE_NULLABLE_TPS)
                await self._db.execute("INSERT INTO trades_new SELECT * FROM trades")
                await self._db.execute("DROP TABLE trades")
                await self._db.execute("ALTER TABLE trades_new RENAME TO trades")
                await self._db.commit()
                logger.info("DB migration: tp1/tp2/tp3/sl are now nullable")
        except Exception as e:
            logger.warning(f"DB migration (nullable TPs) failed: {e}")
        # Retroactively mark resync ghost records as 'dropped' so they
        # don't pollute /history or /stats (exit_price=0, pnl=0 = never really closed)
        await self._db.execute(
            "UPDATE trades SET status='dropped' "
            "WHERE status='closed' AND exit_price=0 AND realized_pnl=0"
        )
        await self._db.commit()
        logger.info('DB cleanup: marked zero-pnl ghost records as dropped')
        logger.info(f"Database connected: {DB_PATH}")

    async def mark_trade_cancelled(self, trade_id: str) -> None:
        """
        Mark a trade as cancelled (entry order was never filled).
        Treated as closed with zero PnL so it falls out of active tracking.
        """
        await self._db.execute(
            "UPDATE trades SET status = 'cancelled', closed_at = ? WHERE trade_id = ?",
            (datetime.utcnow().isoformat(), trade_id),
        )
        await self._db.commit()
        logger.info(f"Trade {trade_id} marked as cancelled (entry never filled)")

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            logger.info("Database connection closed.")

    # ── Write operations ──────────────────────────────────────────────────────

    async def insert_trade(self, trade: TradeRecord) -> None:
        async with self._lock:
            await self._db.execute(
                """
                INSERT INTO trades (
                    trade_id, pair, side, entry, sl, tp1, tp2, tp3,
                    position_size, leverage, risk_amount, balance_at_entry,
                    status, dca, entry_order_id, sl_order_id,
                    tp1_order_id, tp2_order_id, tp3_order_id, dca_order_id,
                    position_id, soft_sl_price, soft_sl_timeframe, strategy, opened_at, closed_at, realized_pnl, exit_price, seen_close_ids
                ) VALUES (
                    :trade_id, :pair, :side, :entry, :sl, :tp1, :tp2, :tp3,
                    :position_size, :leverage, :risk_amount, :balance_at_entry,
                    :status, :dca, :entry_order_id, :sl_order_id,
                    :tp1_order_id, :tp2_order_id, :tp3_order_id, :dca_order_id,
                    :position_id, :soft_sl_price, :soft_sl_timeframe, :strategy, :opened_at, :closed_at, :realized_pnl, :exit_price, :seen_close_ids
                )
                """,
                {
                    "trade_id": trade.trade_id,
                    "pair": trade.pair,
                    "side": trade.side.value,
                    "entry": trade.entry,
                    "sl": trade.sl,
                    "tp1": trade.tp1,
                    "tp2": trade.tp2,
                    "tp3": trade.tp3,
                    "position_size": trade.position_size,
                    "leverage": trade.leverage,
                    "risk_amount": trade.risk_amount,
                    "balance_at_entry": trade.balance_at_entry,
                    "status": trade.status.value,
                    "dca": trade.dca,
                    "entry_order_id": trade.entry_order_id,
                    "sl_order_id": trade.sl_order_id,
                    "tp1_order_id": trade.tp1_order_id,
                    "tp2_order_id": trade.tp2_order_id,
                    "tp3_order_id": trade.tp3_order_id,
                    "dca_order_id": trade.dca_order_id,
                    "position_id": trade.position_id,
                    "soft_sl_price": trade.soft_sl_price,
                    "soft_sl_timeframe": trade.soft_sl_timeframe,
                    "strategy": trade.strategy,
                    "opened_at": trade.opened_at.isoformat(),
                    "closed_at": trade.closed_at.isoformat() if trade.closed_at else None,
                    "realized_pnl": trade.realized_pnl,
                    "exit_price": trade.exit_price,
                    "seen_close_ids": trade.seen_close_ids or "",
                },
            )
            await self._db.commit()
            logger.debug(f"Inserted trade {trade.trade_id}")

    async def update_trade(self, trade: TradeRecord) -> None:
        """Full update of a trade record."""
        async with self._lock:
            await self._db.execute(
                """
                UPDATE trades SET
                    sl              = :sl,
                    status          = :status,
                    sl_order_id     = :sl_order_id,
                    tp1_order_id    = :tp1_order_id,
                    tp2_order_id    = :tp2_order_id,
                    tp3_order_id    = :tp3_order_id,
                    dca_order_id    = :dca_order_id,
                    entry_order_id  = :entry_order_id,
                    position_id     = :position_id,
                    soft_sl_price       = :soft_sl_price,
                    soft_sl_timeframe   = :soft_sl_timeframe,
                    strategy            = :strategy,
                    closed_at       = :closed_at,
                    realized_pnl    = :realized_pnl,
                    exit_price      = :exit_price
                WHERE trade_id = :trade_id
                """,
                {
                    "trade_id": trade.trade_id,
                    "sl": trade.sl,
                    "status": trade.status.value,
                    "sl_order_id": trade.sl_order_id,
                    "tp1_order_id": trade.tp1_order_id,
                    "tp2_order_id": trade.tp2_order_id,
                    "tp3_order_id": trade.tp3_order_id,
                    "dca_order_id": trade.dca_order_id,
                    "entry_order_id": trade.entry_order_id,
                    "position_id": trade.position_id,
                    "soft_sl_price": trade.soft_sl_price,
                    "soft_sl_timeframe": trade.soft_sl_timeframe,
                    "strategy": trade.strategy,
                    "closed_at": trade.closed_at.isoformat() if trade.closed_at else None,
                    "realized_pnl": trade.realized_pnl,
                    "exit_price": trade.exit_price,
                },
            )
            await self._db.commit()
            logger.debug(f"Updated trade {trade.trade_id}")

    async def update_seen_close_ids(self, trade_id: str, seen_ids: str) -> None:
        """Persist the seen_close_ids string for a trade (comma-separated order IDs)."""
        async with self._lock:
            await self._db.execute(
                "UPDATE trades SET seen_close_ids = ? WHERE trade_id = ?",
                (seen_ids, trade_id),
            )
            await self._db.commit()

    async def mark_closed(
        self,
        trade_id: str,
        realized_pnl: float = 0.0,
        exit_price: float = 0.0,
    ) -> None:
        async with self._lock:
            await self._db.execute(
                """
                UPDATE trades SET
                    status       = 'closed',
                    closed_at    = :closed_at,
                    realized_pnl = :realized_pnl,
                    exit_price   = :exit_price
                WHERE trade_id = :trade_id
                """,
                {
                    "trade_id": trade_id,
                    "closed_at": datetime.utcnow().isoformat(),
                    "realized_pnl": realized_pnl,
                    "exit_price": exit_price,
                },
            )
            await self._db.commit()

    # ── Read operations ───────────────────────────────────────────────────────

    async def get_active_trades(self) -> dict[str, TradeRecord]:
        """Return all trades with status='open', keyed by pair."""
        cursor = await self._db.execute(
            "SELECT * FROM trades WHERE status = 'open' OR status = 'partial'"
        )
        rows = await cursor.fetchall()
        trades = [_row_to_trade(r) for r in rows]
        return {t.pair: t for t in trades}

    async def get_trade(self, trade_id: str) -> Optional[TradeRecord]:
        cursor = await self._db.execute(
            "SELECT * FROM trades WHERE trade_id = ?", (trade_id,)
        )
        row = await cursor.fetchone()
        return _row_to_trade(row) if row else None

    async def get_trade_by_pair(self, pair: str) -> Optional[TradeRecord]:
        cursor = await self._db.execute(
            "SELECT * FROM trades WHERE pair = ? AND (status = 'open' OR status = 'partial') LIMIT 1",
            (pair,),
        )
        row = await cursor.fetchone()
        return _row_to_trade(row) if row else None

    async def get_trade_history(self, limit: int = 50) -> list[TradeRecord]:
        cursor = await self._db.execute(
            "SELECT * FROM trades ORDER BY opened_at DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
        return [_row_to_trade(r) for r in rows]

    async def get_closed_trades(self, limit: int = 50) -> list[TradeRecord]:
        cursor = await self._db.execute(
            "SELECT * FROM trades WHERE status = 'closed' "
            "AND status NOT IN ('dropped', 'cancelled') "
            "ORDER BY closed_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [_row_to_trade(r) for r in rows]

    async def get_stats(self) -> dict:
        """Return aggregate trading statistics."""
        cursor = await self._db.execute(
            """
            SELECT
                COUNT(*)                                        AS total_trades,
                SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) AS winning_trades,
                SUM(CASE WHEN realized_pnl < 0 THEN 1 ELSE 0 END) AS losing_trades,
                SUM(realized_pnl)                              AS total_pnl,
                AVG(realized_pnl)                              AS avg_pnl,
                MAX(realized_pnl)                              AS best_trade,
                MIN(realized_pnl)                              AS worst_trade
            FROM trades
            WHERE status = 'closed'
            AND status NOT IN ('dropped', 'cancelled')
            """
        )
        row = await cursor.fetchone()
        if not row:
            return {}
        total = row["total_trades"] or 0
        wins = row["winning_trades"] or 0
        win_rate = (wins / total * 100) if total > 0 else 0
        return {
            "total_trades": total,
            "winning_trades": wins,
            "losing_trades": row["losing_trades"] or 0,
            "win_rate": round(win_rate, 1),
            "total_pnl": round(row["total_pnl"] or 0, 2),
            "avg_pnl": round(row["avg_pnl"] or 0, 2),
            "best_trade": round(row["best_trade"] or 0, 2),
            "worst_trade": round(row["worst_trade"] or 0, 2),
        }