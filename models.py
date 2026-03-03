from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
from datetime import datetime


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class TradeStatus(str, Enum):
    PENDING = "pending"
    OPEN = "open"
    PARTIAL = "partial"
    CLOSED = "closed"
    CANCELLED = "cancelled"
    DROPPED = "dropped"  # resync-dropped — excluded from stats/history


class RiskType(str, Enum):
    PERCENT = "percent"
    DOLLAR = "dollar"
    TOKENS = "tokens"


@dataclass
class TradeRequest:
    pair: str
    side: Side
    entry: float | str  # float or "market"
    risk_value: float
    risk_type: RiskType
    sl: float
    tp1: Optional[float] = None
    tp2: Optional[float] = None
    tp3: Optional[float] = None
    sl_timeframe: str = "1h"   # timeframe for soft SL candle monitoring
    dca: Optional[float] = None
    strategy: Optional[str] = None  # "neil" | "saltwayer" | None
    dca_qty: Optional[float] = None  # override DCA qty (neil/saltwayer auto-sizing)

    @property
    def is_market(self) -> bool:
        return self.entry == "market"


@dataclass
class PositionSizing:
    balance: float
    risk_amount: float
    entry_price: float
    sl_price: float
    stop_distance: float
    position_size: float
    leverage: int
    margin_required: float
    liquidation_price: float
    risk_percent: float
    qty_precision: int = 4   # exchange basePrecision for this symbol
    min_qty: float = 0.0       # exchange minimum order quantity


@dataclass
class TradeRecord:
    trade_id: str
    pair: str
    side: Side
    entry: float
    sl: float
    position_size: float
    leverage: int
    risk_amount: float
    balance_at_entry: float
    tp1: float = 0.0
    tp2: float = 0.0
    tp3: float = 0.0
    status: TradeStatus = TradeStatus.OPEN
    dca: Optional[float] = None
    strategy: Optional[str] = None  # "neil" | "saltwayer" | None
    entry_order_id: Optional[str] = None
    sl_order_id: Optional[str] = None
    tp1_order_id: Optional[str] = None
    tp2_order_id: Optional[str] = None
    tp3_order_id: Optional[str] = None
    dca_order_id: Optional[str] = None
    position_id:        Optional[str]   = None   # Bybit positionIdx for trading-stop (always "0" in one-way mode)
    # Soft SL — monitored by SoftSLMonitor, no exchange order placed
    soft_sl_price:      Optional[float] = None   # price level to watch
    soft_sl_timeframe:  Optional[str]   = None   # '15m'|'30m'|'1h'|'4h'|'Daily'
    opened_at: datetime = field(default_factory=datetime.utcnow)
    closed_at: Optional[datetime] = None
    realized_pnl: float = 0.0
    exit_price: float = 0.0


@dataclass
class BybitPosition:
    symbol: str
    side: str
    size: float
    entry_price: float
    unrealized_pnl: float
    leverage: int
    margin: float
    position_id: str = ""   # Bitunix positionId


class BotException(Exception):
    pass


class ValidationError(BotException):
    pass


class APIError(BotException):
    def __init__(self, message: str, status_code: int = 0, response: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.response = response


class InsufficientMarginError(BotException):
    pass


class DuplicateTradeError(BotException):
    pass