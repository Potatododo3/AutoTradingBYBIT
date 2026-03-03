"""
Bybit Futures REST API client — v5 (USDT Linear Perpetuals).
Docs: https://bybit-exchange.github.io/docs/v5/intro
Base URL: https://api.bybit.com

Authentication (HMAC-SHA256):
  sign_string = timestamp + api_key + recv_window + payload
  payload = URL-encoded query string (GET) | compact JSON body (POST)
  signature = HMAC-SHA256(secret, sign_string).hexdigest()
  Headers: X-BAPI-API-KEY, X-BAPI-TIMESTAMP, X-BAPI-RECV-WINDOW, X-BAPI-SIGN

Response envelope:
  {"retCode": 0, "retMsg": "OK", "result": {...}}
  retCode != 0 → raise APIError.

Key differences from Bitunix:
  - side is "Buy"/"Sell" (title case), not "BUY"/"SELL"
  - orderType is "Market"/"Limit" (title case)
  - No tradeSide/effect params; closing orders use reduceOnly=True
  - No separate positionId — positionIdx ("0" one-way) stored as position_id
  - TP/SL set via POST /v5/position/trading-stop (not a separate order system)
  - Kline returns newest-first array-of-arrays
  - Order status field is "orderStatus"; filled qty is "cumExecQty"
  - No flash_close endpoint — we market-close via /v5/order/create
  - get_pending_tpsl returns [] (TP/SL lives in position data on Bybit)
"""
import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

from config import BYBIT_API_KEY, BYBIT_SECRET, BYBIT_BASE_URL, API_RETRY_ATTEMPTS, API_TIMEOUT
from models import APIError, BybitPosition

logger = logging.getLogger(__name__)

_RECV_WINDOW = "5000"
_CATEGORY    = "linear"   # USDT linear perpetuals


# ── Signing ───────────────────────────────────────────────────────────────────

def _sign(secret: str, timestamp: str, api_key: str, recv_window: str, payload: str) -> str:
    """
    Bybit v5 HMAC-SHA256 signature.
    sign_string = timestamp + api_key + recv_window + payload
      GET  payload = URL-encoded query string (e.g. "category=linear&symbol=BTCUSDT")
      POST payload = compact JSON body (no spaces)
    """
    raw = timestamp + api_key + recv_window + payload
    return hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()


# ── Client ────────────────────────────────────────────────────────────────────

class BybitClient:
    def __init__(self) -> None:
        self._http = httpx.AsyncClient(
            base_url=BYBIT_BASE_URL,
            timeout=API_TIMEOUT,
        )

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        body: Optional[dict] = None,
    ) -> Any:
        """
        Execute a signed Bybit v5 request.
        Returns the `result` field on success.
        Raises APIError on non-zero retCode, timeout, or network failure.
        """
        for attempt in range(1, API_RETRY_ATTEMPTS + 1):
            try:
                timestamp = str(int(time.time() * 1000))

                # Payload to sign differs by HTTP method:
                #   GET  → URL-encoded query string (same dict as params)
                #   POST → compact JSON body string
                if method.upper() == "GET":
                    payload = urlencode(params) if params else ""
                else:
                    payload = json.dumps(body, separators=(",", ":")) if body else ""

                signature = _sign(BYBIT_SECRET, timestamp, BYBIT_API_KEY, _RECV_WINDOW, payload)

                headers = {
                    "X-BAPI-API-KEY":     BYBIT_API_KEY,
                    "X-BAPI-TIMESTAMP":   timestamp,
                    "X-BAPI-RECV-WINDOW": _RECV_WINDOW,
                    "X-BAPI-SIGN":        signature,
                    "Content-Type":       "application/json",
                }

                logger.debug(f"→ {method} {path} params={params} body={payload[:200] or 'none'}")

                response = await self._http.request(
                    method,
                    path,
                    params=params,
                    content=payload.encode() if method.upper() != "GET" and body else None,
                    headers=headers,
                )

                raw = response.json()
                logger.debug(f"← {response.status_code} {json.dumps(raw)[:400]}")

                ret_code = raw.get("retCode", -1)
                if ret_code != 0:
                    raise APIError(
                        message=raw.get("retMsg", "Unknown Bybit API error"),
                        status_code=response.status_code,
                        response=json.dumps(raw),
                    )
                return raw.get("result")

            except APIError:
                raise
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                logger.warning(f"Network error attempt {attempt}/{API_RETRY_ATTEMPTS}: {e}")
                if attempt == API_RETRY_ATTEMPTS:
                    raise APIError(f"Network error after {API_RETRY_ATTEMPTS} retries: {e}")
                await asyncio.sleep(2 ** attempt)
            except Exception as e:
                logger.error(f"Unexpected error in _request: {e}", exc_info=True)
                raise APIError(f"Unexpected error: {e}")

    # ── Account ───────────────────────────────────────────────────────────────
    # GET /v5/account/wallet-balance?accountType=UNIFIED
    # result.list[0]: totalEquity, totalAvailableBalance
    # coin[]: {coin, equity, availableToWithdraw, ...}

    async def _get_wallet(self) -> dict:
        result = await self._request("GET", "/v5/account/wallet-balance",
                                     params={"accountType": "UNIFIED"})
        lst = result.get("list", []) if isinstance(result, dict) else []
        return lst[0] if lst else {}

    async def get_balance(self) -> float:
        """Available USDT balance (free margin, not in use)."""
        wallet = await self._get_wallet()
        raw = wallet.get("totalAvailableBalance", "")
        if raw and raw != "":
            val = float(raw)
            logger.debug(f"get_balance → {val}")
            return val
        # Fallback: find USDT coin entry
        for coin in wallet.get("coin", []):
            if coin.get("coin") == "USDT":
                val = float(coin.get("availableToWithdraw", 0) or 0)
                logger.debug(f"get_balance (coin fallback) → {val}")
                return val
        return 0.0

    async def get_total_balance(self) -> float:
        """Total equity = wallet balance + unrealised PnL."""
        wallet = await self._get_wallet()
        raw = wallet.get("totalEquity", "")
        if raw and raw != "":
            val = float(raw)
            logger.debug(f"get_total_balance → {val}")
            return val
        for coin in wallet.get("coin", []):
            if coin.get("coin") == "USDT":
                val = float(coin.get("equity", 0) or 0)
                logger.debug(f"get_total_balance (coin fallback) → {val}")
                return val
        return 0.0

    # ── Market ────────────────────────────────────────────────────────────────
    # GET /v5/market/tickers?category=linear&symbol=BTCUSDT
    # result.list[0].lastPrice

    async def get_ticker_price(self, symbol: str) -> float:
        result = await self._request("GET", "/v5/market/tickers",
                                     params={"category": _CATEGORY, "symbol": symbol})
        lst = result.get("list", []) if isinstance(result, dict) else []
        if lst:
            price = float(lst[0].get("lastPrice", 0))
            logger.debug(f"get_ticker_price {symbol} → {price}")
            return price
        raise APIError(f"Could not get ticker price for {symbol}")

    # ── Leverage ──────────────────────────────────────────────────────────────
    # POST /v5/position/set-leverage
    # Body: {category, symbol, buyLeverage (str), sellLeverage (str)}
    # Both sides must be provided as strings even in one-way mode.

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        lev = str(leverage)
        await self._request("POST", "/v5/position/set-leverage", body={
            "category":     _CATEGORY,
            "symbol":       symbol,
            "buyLeverage":  lev,
            "sellLeverage": lev,
        })
        logger.info(f"Leverage set: {symbol} → {leverage}x")

    # ── Positions ─────────────────────────────────────────────────────────────
    # GET /v5/position/list?category=linear&symbol=X  (or settleCoin=USDT for all)
    # result.list[n]: symbol, side ("Buy"/"Sell"), size, avgPrice, unrealisedPnl,
    #                 leverage, positionIdx (0=one-way), positionIM (initial margin)
    #
    # IMPORTANT: Bybit has no separate positionId like Bitunix.
    # We store positionIdx as position_id. The /v5/position/trading-stop endpoint
    # takes positionIdx — so callers passing position_id to place_tpsl and
    # modify_position_tpsl will get the right value.

    @staticmethod
    def _normalise_side(raw: str) -> str:
        """Bybit returns 'Buy'/'Sell'. Normalise to 'LONG'/'SHORT'."""
        s = str(raw).strip()
        if s in ("Buy", "BUY", "LONG"):
            return "LONG"
        if s in ("Sell", "SELL", "SHORT"):
            return "SHORT"
        logger.warning(f"Unknown position side: {raw!r}")
        return s.upper()

    def _parse_position(self, p: dict) -> "BybitPosition":
        return BybitPosition(
            symbol         = p.get("symbol", ""),
            side           = self._normalise_side(p.get("side", "")),
            size           = float(p.get("size",          0) or 0),
            entry_price    = float(p.get("avgPrice",      0) or 0),
            unrealized_pnl = float(p.get("unrealisedPnl", 0) or 0),
            leverage       = int(float(p.get("leverage",  1) or 1)),
            margin         = float(p.get("positionIM",    0) or 0),
            # positionIdx is what trading-stop needs as "positionIdx"
            position_id    = str(p.get("positionIdx", 0)),
        )

    async def get_positions(self) -> list["BybitPosition"]:
        """All open positions (size > 0)."""
        result = await self._request("GET", "/v5/position/list",
                                     params={"category": _CATEGORY, "settleCoin": "USDT"})
        lst = result.get("list", []) if isinstance(result, dict) else []
        positions = [self._parse_position(p) for p in lst if float(p.get("size", 0) or 0) > 0]
        logger.debug(f"get_positions → {len(positions)} open position(s)")
        return positions

    async def get_position(self, symbol: str) -> Optional["BybitPosition"]:
        """Single open position for a symbol, or None if flat."""
        result = await self._request("GET", "/v5/position/list",
                                     params={"category": _CATEGORY, "symbol": symbol})
        lst = result.get("list", []) if isinstance(result, dict) else []
        for p in lst:
            if float(p.get("size", 0) or 0) > 0:
                return self._parse_position(p)
        return None

    # ── Orders ────────────────────────────────────────────────────────────────
    # POST /v5/order/create
    # Body: {category, symbol, side ("Buy"/"Sell"), orderType ("Market"/"Limit"),
    #        qty (str), price? (str), timeInForce?, reduceOnly? (bool), positionIdx (int)}
    # No tradeSide or effect params on Bybit. Closing orders use reduceOnly=True.
    # result: {orderId, orderLinkId}

    @staticmethod
    def _bybit_side(side: str) -> str:
        """'BUY'/'LONG' → 'Buy',  'SELL'/'SHORT' → 'Sell'."""
        return "Buy" if side.upper() in ("BUY", "LONG") else "Sell"

    @staticmethod
    def _bybit_order_type(order_type: str) -> str:
        """'MARKET' → 'Market',  'LIMIT' → 'Limit'."""
        return "Market" if order_type.upper() == "MARKET" else "Limit"

    async def place_order(
        self,
        symbol:      str,
        side:        str,            # "BUY" | "SELL"
        order_type:  str,            # "MARKET" | "LIMIT"
        qty:         float,
        price:       Optional[float] = None,
        reduce_only: bool = False,
        trade_side:  str  = "OPEN",  # kept for call-site compat; maps to reduceOnly
        position_id: Optional[str] = None,  # positionIdx ("0","1","2")
    ) -> str:
        """Place an order and return orderId."""
        b_side      = self._bybit_side(side)
        b_ordertype = self._bybit_order_type(order_type)

        body: dict[str, Any] = {
            "category":    _CATEGORY,
            "symbol":      symbol,
            "side":        b_side,
            "orderType":   b_ordertype,
            "qty":         str(qty),
            "timeInForce": "GTC",
        }
        if price is not None and b_ordertype == "Limit":
            body["price"] = str(price)
        # Bybit uses reduceOnly=True where Bitunix used tradeSide="CLOSE"
        if reduce_only or trade_side.upper() == "CLOSE":
            body["reduceOnly"] = True
        # positionIdx: 0 = one-way mode (default for all our trades)
        try:
            body["positionIdx"] = int(position_id) if position_id is not None else 0
        except (TypeError, ValueError):
            body["positionIdx"] = 0

        logger.info(f"place_order → body: {json.dumps(body)}")
        result   = await self._request("POST", "/v5/order/create", body=body)
        order_id = str(result.get("orderId", "")) if isinstance(result, dict) else ""
        logger.info(f"place_order {b_ordertype} {b_side} {qty} {symbol} → orderId={order_id}")
        return order_id

    # POST /v5/order/cancel
    # Body: {category, symbol, orderId}

    async def cancel_order(self, symbol: str, order_id: str) -> None:
        await self._request("POST", "/v5/order/cancel", body={
            "category": _CATEGORY,
            "symbol":   symbol,
            "orderId":  order_id,
        })
        logger.info(f"Cancelled order {order_id} for {symbol}")

    # POST /v5/order/cancel-all
    # Body: {category, symbol}

    async def cancel_all_orders(self, symbol: str) -> None:
        await self._request("POST", "/v5/order/cancel-all", body={
            "category": _CATEGORY,
            "symbol":   symbol,
        })
        logger.info(f"Cancelled all orders for {symbol}")

    # GET /v5/order/realtime?category=linear&symbol=X
    # result.list — open / partially-filled orders

    async def get_open_orders(self, symbol: Optional[str] = None) -> list[dict]:
        params: dict = {"category": _CATEGORY}
        if symbol:
            params["symbol"] = symbol
        result = await self._request("GET", "/v5/order/realtime", params=params)
        return result.get("list", []) if isinstance(result, dict) else []

    # ── TP/SL via trading-stop ────────────────────────────────────────────────
    # POST /v5/position/trading-stop
    # Body: {category, symbol, takeProfit (str), stopLoss (str), positionIdx (int)}
    # This creates or modifies position-level TP/SL — there is no separate order id.
    # Both place_tpsl and modify_position_tpsl use this same endpoint.

    async def _set_trading_stop(
        self,
        symbol:       str,
        position_idx: int,
        tp_price:     Optional[float],
        sl_price:     Optional[float],
    ) -> None:
        body: dict = {
            "category":    _CATEGORY,
            "symbol":      symbol,
            "positionIdx": position_idx,
        }
        if tp_price is not None and tp_price > 0:
            body["takeProfit"] = str(tp_price)
        if sl_price is not None and sl_price > 0:
            body["stopLoss"] = str(sl_price)
        await self._request("POST", "/v5/position/trading-stop", body=body)
        logger.info(f"trading-stop {symbol} posIdx={position_idx} tp={tp_price} sl={sl_price}")

    async def place_tpsl(
        self,
        symbol:      str,
        position_id: str,           # positionIdx as string ("0","1","2")
        tp_price:    Optional[float] = None,
        sl_price:    Optional[float] = None,
    ) -> str:
        """Set position-level TP/SL. Returns '' (no separate order id on Bybit)."""
        try:
            pos_idx = int(position_id)
        except (TypeError, ValueError):
            pos_idx = 0
        await self._set_trading_stop(symbol, pos_idx, tp_price, sl_price)
        return ""

    async def modify_position_tpsl(
        self,
        symbol:      str,
        position_id: str,
        tp_price:    Optional[float] = None,
        sl_price:    Optional[float] = None,
    ) -> None:
        """Modify existing TP/SL — same endpoint as place_tpsl on Bybit."""
        await self.place_tpsl(symbol, position_id, tp_price, sl_price)

    # Bybit has no separate pending-tpsl list. TP/SL is embedded in the position.
    # Return [] so existing callers that iterate pending_tpsl do nothing.
    async def get_pending_tpsl(self, symbol: Optional[str] = None) -> list[dict]:
        return []

    async def get_pending_orders_raw(self, symbol: Optional[str] = None) -> list[dict]:
        return await self.get_open_orders(symbol)

    # ── Klines ────────────────────────────────────────────────────────────────
    # GET /v5/market/kline?category=linear&symbol=X&interval=Y&limit=N
    # result.list — array-of-arrays, newest first:
    #   [startTime (ms str), open, high, low, close, volume, turnover]
    # Bybit interval strings: 1,3,5,15,30,60,120,240,360,480,720,D,W,M

    _INTERVAL_MAP: dict[str, str] = {
        "1m":    "1",
        "3m":    "3",
        "5m":    "5",
        "15m":   "15",
        "30m":   "30",
        "1h":    "60",
        "2h":    "120",
        "4h":    "240",
        "6h":    "360",
        "8h":    "480",
        "12h":   "720",
        "1d":    "D",
        "Daily": "D",
        "1w":    "W",
        "1M":    "M",
    }

    async def get_klines(self, symbol: str, interval: str, limit: int = 2) -> list[dict]:
        """
        Returns list of dicts {time, open, high, low, close} oldest-first.
        Last element is the still-forming candle (same semantics as Bitunix version).
        """
        bybit_interval = self._INTERVAL_MAP.get(interval, interval)
        result = await self._request("GET", "/v5/market/kline", params={
            "category": _CATEGORY,
            "symbol":   symbol,
            "interval": bybit_interval,
            "limit":    limit,
        })
        raw_list = result.get("list", []) if isinstance(result, dict) else []
        candles  = []
        for c in raw_list:
            # c = [startTime, open, high, low, close, volume, turnover]
            try:
                candles.append({
                    "time":  int(c[0]),
                    "open":  float(c[1]),
                    "high":  float(c[2]),
                    "low":   float(c[3]),
                    "close": float(c[4]),
                })
            except (IndexError, TypeError, ValueError) as e:
                logger.warning(f"get_klines bad candle {c}: {e}")
        candles.reverse()   # Bybit is newest-first; we need oldest-first
        logger.debug(f"get_klines {symbol}/{interval} → {len(candles)} candle(s)")
        return candles

    # ── Symbol info ───────────────────────────────────────────────────────────
    # GET /v5/market/instruments-info?category=linear&symbol=X
    # result.list[0]:
    #   lotSizeFilter.qtyStep      → qty precision step  e.g. "0.001"
    #   lotSizeFilter.minOrderQty  → minimum order qty   e.g. "0.001"
    #   priceFilter.tickSize       → price step          e.g. "0.10"
    #   leverageFilter.maxLeverage → "100.00"
    #   status                     → "Trading" | "Settling" | "Closed"

    @staticmethod
    def _decimals(step_str: str) -> int:
        """Count decimal places: '0.001' → 3, '0.10' → 1, '1' → 0."""
        s = str(step_str)
        if "." in s:
            return len(s.split(".")[1].rstrip("0")) or 0
        return 0

    async def get_symbol_info(self, symbol: str) -> dict:
        """
        Returns a dict compatible with the Bitunix interface:
          basePrecision  (int)   — decimal places derived from qtyStep
          quotePrecision (int)   — decimal places derived from tickSize
          minTradeVolume (float) — minOrderQty
          symbolStatus   (str)   — "OPEN" | "CLOSED"
        Falls back to safe defaults on any error.
        """
        defaults = {
            "basePrecision":  4,
            "quotePrecision": 2,
            "minTradeVolume": 0.001,
            "symbolStatus":   "OPEN",
        }
        try:
            result = await self._request("GET", "/v5/market/instruments-info",
                                         params={"category": _CATEGORY, "symbol": symbol})
            lst = result.get("list", []) if isinstance(result, dict) else []
            if not lst:
                logger.warning(f"get_symbol_info: no data for {symbol}, using defaults")
                return defaults
            info      = lst[0]
            lot       = info.get("lotSizeFilter", {})
            pf        = info.get("priceFilter",  {})
            qty_step  = lot.get("qtyStep",     "0.001")
            min_qty   = lot.get("minOrderQty", "0.001")
            tick_size = pf.get("tickSize",     "0.01")
            status    = info.get("status", "Trading")
            res = {
                "basePrecision":  self._decimals(qty_step),
                "quotePrecision": self._decimals(tick_size),
                "minTradeVolume": float(min_qty),
                "symbolStatus":   "OPEN" if status == "Trading" else "CLOSED",
            }
            logger.info(
                f"Symbol info {symbol}: qty_prec={res['basePrecision']} "
                f"price_prec={res['quotePrecision']} min_qty={min_qty} status={status}"
            )
            return res
        except Exception as e:
            logger.warning(f"get_symbol_info({symbol}) failed: {e} — using defaults")
        return defaults

    # ── Max leverage ──────────────────────────────────────────────────────────

    async def get_max_leverage(self, symbol: str) -> int:
        """Max leverage from leverageFilter.maxLeverage in instruments-info. Fallback: 20."""
        try:
            result = await self._request("GET", "/v5/market/instruments-info",
                                         params={"category": _CATEGORY, "symbol": symbol})
            lst = result.get("list", []) if isinstance(result, dict) else []
            if lst:
                lf      = lst[0].get("leverageFilter", {})
                max_lev = int(float(lf.get("maxLeverage", 20)))
                logger.info(f"Max leverage for {symbol}: {max_lev}x")
                return max_lev
        except Exception as e:
            logger.warning(f"get_max_leverage({symbol}) failed: {e} — falling back to 20x")
        return 20

    # ── Order status ──────────────────────────────────────────────────────────
    # GET /v5/order/history?category=linear&orderId=X&symbol=X
    # result.list[0]: orderStatus, qty, cumExecQty, avgPrice
    #
    # Bybit orderStatus values → normalised:
    #   New → NEW, PartiallyFilled → PART_FILLED, Filled → FILLED,
    #   Cancelled/Rejected/Deactivated → CANCELLED, Triggered → FILLED, Untriggered → NEW

    _STATUS_MAP: dict[str, str] = {
        "New":             "NEW",
        "PartiallyFilled": "PART_FILLED",
        "Filled":          "FILLED",
        "Cancelled":       "CANCELLED",
        "Rejected":        "CANCELLED",
        "Deactivated":     "CANCELLED",
        "Triggered":       "FILLED",
        "Untriggered":     "NEW",
    }

    async def get_order_status(self, order_id: str, symbol: str = "") -> dict:
        """
        Returns dict: {status (normalised), qty, tradeQty, price}.
        Returns {} if not found.
        """
        try:
            params: dict = {"category": _CATEGORY, "orderId": order_id}
            if symbol:
                params["symbol"] = symbol
            result = await self._request("GET", "/v5/order/history", params=params)
            lst    = result.get("list", []) if isinstance(result, dict) else []
            if lst:
                o           = lst[0]
                raw_status  = o.get("orderStatus", "")
                norm_status = self._STATUS_MAP.get(raw_status, raw_status.upper())
                out = {
                    "status":   norm_status,
                    "qty":      o.get("qty",        "0"),
                    "tradeQty": o.get("cumExecQty", "0"),   # mapped to Bitunix field name
                    "price":    o.get("avgPrice",   "0"),
                    "orderId":  o.get("orderId",    order_id),
                }
                logger.debug(f"get_order_status {order_id} → {norm_status}")
                return out
        except Exception as e:
            logger.warning(f"get_order_status({order_id}) failed: {e}")
        return {}

    # ── History orders ────────────────────────────────────────────────────────
    # GET /v5/order/history?category=linear&symbol=X&limit=N
    # result.list each order: orderId, symbol, qty, cumExecQty, avgPrice,
    #   side ("Buy"/"Sell"), orderType, orderStatus, reduceOnly (bool),
    #   createdTime (ms str), updatedTime (ms str), closedPnl
    #
    # order_manager checks `tradeSide` field from history orders to classify
    # close vs open orders. Bybit doesn't have tradeSide; it uses reduceOnly.
    # We emit tradeSide="CLOSE" when reduceOnly=True so existing logic works.

    async def get_history_orders(
        self,
        symbol:     str,
        limit:      int = 50,
        start_time: Optional[int] = None,
    ) -> list[dict]:
        """
        Returns normalised order list compatible with order_manager logic.
        Bybit → internal field mapping:
          orderStatus → status  (normalised to FILLED/CANCELLED/etc)
          cumExecQty  → tradeQty
          side        → side  (uppercased: "BUY"/"SELL")
          createdTime → ctime (ms int)
          updatedTime → mtime (ms int)
          reduceOnly  → tradeSide ("CLOSE" if True, else "OPEN")
        """
        params: dict = {"category": _CATEGORY, "symbol": symbol, "limit": limit}
        result = await self._request("GET", "/v5/order/history", params=params)
        lst    = result.get("list", []) if isinstance(result, dict) else []

        normalised = []
        for o in lst:
            raw_status  = o.get("orderStatus", "")
            reduce_only = o.get("reduceOnly", False)
            norm = {
                "orderId":     o.get("orderId", ""),
                "symbol":      o.get("symbol", symbol),
                "status":      self._STATUS_MAP.get(raw_status, raw_status.upper()),
                "side":        o.get("side", "").upper(),           # "BUY" / "SELL"
                "orderType":   o.get("orderType", "").upper(),      # "LIMIT" / "MARKET"
                "qty":         o.get("qty",        "0"),
                "tradeQty":    o.get("cumExecQty", "0"),
                "price":       o.get("avgPrice", o.get("price", "0")),
                "realizedPNL": o.get("closedPnl", "0") or "0",
                # Emit tradeSide so order_manager's tradeSide checks work unchanged
                "tradeSide":   "CLOSE" if reduce_only else "OPEN",
                "ctime":       int(o.get("createdTime", 0) or 0),
                "mtime":       int(o.get("updatedTime",  0) or 0),
            }
            normalised.append(norm)

        if start_time and normalised:
            normalised = [o for o in normalised if o["ctime"] >= start_time]

        logger.debug(f"get_history_orders {symbol} → {len(normalised)} order(s)")
        return normalised

    # ── History position (closed PnL) ─────────────────────────────────────────
    # GET /v5/position/closed-pnl?category=linear&symbol=X&limit=1
    # result.list[0]: closedPnl, avgEntryPrice, avgExitPrice, side, createdTime
    #
    # Bybit has no get-by-positionId. We query by symbol and return latest close.

    async def get_history_position(self, position_id: str, symbol: str = "") -> Optional[dict]:
        """
        Returns dict with fields matching what order_manager reads:
          realizedPNL, entryPrice, closePrice, side, ctime, positionId.
        symbol is required (positionIdx is not unique enough to query by).
        Returns None if not found.
        """
        if not symbol:
            logger.warning("get_history_position: symbol required on Bybit")
            return None
        try:
            result = await self._request("GET", "/v5/position/closed-pnl",
                                         params={"category": _CATEGORY, "symbol": symbol, "limit": 1})
            lst = result.get("list", []) if isinstance(result, dict) else []
            if lst:
                p = lst[0]
                return {
                    "positionId":  position_id,
                    "symbol":      symbol,
                    "realizedPNL": float(p.get("closedPnl",     0) or 0),
                    "entryPrice":  float(p.get("avgEntryPrice", 0) or 0),
                    "closePrice":  float(p.get("avgExitPrice",  0) or 0),
                    "side":        p.get("side", "").upper(),
                    "ctime":       int(p.get("createdTime", 0) or 0),
                }
        except Exception as e:
            logger.warning(f"get_history_position({symbol}) failed: {e}")
        return None

    # ── Flash / market close ──────────────────────────────────────────────────
    # Bybit has no dedicated flash_close_position endpoint.
    # We look up the current position size and place a market reduceOnly order.

    async def flash_close_position(
        self,
        position_id: str,       # positionIdx — used as positionIdx on the order
        symbol: str = "",
        side:   str = "",
    ) -> None:
        """
        Market-close the full open position for symbol.
        symbol is required on Bybit (there is no global positionId lookup).
        """
        if not symbol:
            raise APIError("flash_close_position requires symbol on Bybit")
        pos = await self.get_position(symbol)
        if not pos or pos.size <= 0:
            logger.warning(f"flash_close_position: no open position for {symbol}")
            return
        close_side = "Sell" if pos.side == "LONG" else "Buy"
        try:
            pos_idx = int(position_id)
        except (TypeError, ValueError):
            pos_idx = 0
        body: dict[str, Any] = {
            "category":    _CATEGORY,
            "symbol":      symbol,
            "side":        close_side,
            "orderType":   "Market",
            "qty":         str(pos.size),
            "timeInForce": "GTC",
            "reduceOnly":  True,
            "positionIdx": pos_idx,
        }
        result   = await self._request("POST", "/v5/order/create", body=body)
        order_id = str(result.get("orderId", "")) if isinstance(result, dict) else ""
        logger.info(f"flash_close_position {symbol} qty={pos.size} → orderId={order_id}")

    async def close_position(
        self,
        symbol:      str,
        side:        str,
        qty:         float,
        position_id: Optional[str] = None,
    ) -> str:
        """Market close via place_order (fallback path)."""
        close_side = "SELL" if side.upper() in ("LONG", "BUY") else "BUY"
        return await self.place_order(
            symbol=symbol,
            side=close_side,
            order_type="MARKET",
            qty=qty,
            reduce_only=True,
            position_id=position_id,
        )

    async def close(self) -> None:
        await self._http.aclose()


# Backwards-compat alias — any file still importing BitunixClient gets BybitClient
BitunixClient = BybitClient