import logging
from typing import Optional

from bybit_client import BybitClient
from config import MAX_LEVERAGE, DEFAULT_LEVERAGE
from models import (
    TradeRequest, PositionSizing, Side,
    InsufficientMarginError, ValidationError
)

logger = logging.getLogger(__name__)


class RiskManager:
    def __init__(self, client: BybitClient) -> None:
        self._client = client

    async def calculate_position(self, req: TradeRequest) -> PositionSizing:
        """
        Calculate full position sizing.
        - Balance = total equity (available + margin + unrealised PnL)
        - Leverage = always MAX_LEVERAGE (set in config / settings)
        - Position size = risk_amount / stop_distance
        """
        from models import RiskType
        from settings_handler import get_settings

        # Risk balance: use fixed amount from settings if set, else live equity
        _s = get_settings()
        equity = await self._client.get_total_balance()
        if equity <= 0:
            raise InsufficientMarginError("Futures wallet equity is zero or unavailable.")

        # risk_base is what % risk is calculated against
        # 0 = live equity (default), >0 = fixed user-defined balance
        risk_base = _s.risk_balance if _s.risk_balance > 0 else equity
        logger.info(f"Risk base: ${risk_base:,.2f} "
                    f"({'fixed' if _s.risk_balance > 0 else 'live equity'}) "
                    f"| live equity: ${equity:,.2f}")

        # Available margin for the actual margin check
        available = await self._client.get_balance()

        # Resolve entry price
        if req.is_market:
            entry_price = await self._client.get_ticker_price(req.pair)
        else:
            entry_price = float(req.entry)

        sl_price = float(req.sl)

        # Validate direction
        if req.side == Side.LONG and sl_price >= entry_price:
            raise ValidationError(f"SL ({sl_price}) must be below entry ({entry_price}) for LONG.")
        if req.side == Side.SHORT and sl_price <= entry_price:
            raise ValidationError(f"SL ({sl_price}) must be above entry ({entry_price}) for SHORT.")

        # Risk amount based on risk_base (fixed or live equity)
        if req.risk_type == RiskType.PERCENT:
            risk_amount = risk_base * (req.risk_value / 100.0)
        else:
            risk_amount = req.risk_value

        if risk_amount <= 0:
            raise ValidationError("Risk amount must be greater than zero.")
        if risk_amount > equity:
            raise ValidationError(f"Risk ${risk_amount:.2f} exceeds equity ${equity:.2f}.")

        stop_distance = abs(entry_price - sl_price)
        if stop_distance == 0:
            raise ValidationError("Entry and SL prices cannot be identical.")

        # Fetch symbol info: precision, min qty, status, and max leverage
        s = get_settings()
        sym_info = await self._client.get_symbol_info(req.pair)
        symbol_max_leverage = await self._client.get_max_leverage(req.pair)
        leverage = min(s.max_leverage, symbol_max_leverage)
        logger.info(f"Leverage: configured={s.max_leverage}x symbol_max={symbol_max_leverage}x using={leverage}x")

        # Reject if symbol is not open for trading
        if sym_info["symbolStatus"] != "OPEN":
            raise ValidationError(
                f"{req.pair} is not currently open for trading (status: {sym_info['symbolStatus']})."
            )

        # ── Position sizing ──────────────────────────────────────────────────
        qty_precision = sym_info["basePrecision"]
        min_qty       = sym_info["minTradeVolume"]

        dca_price = float(req.dca) if req.dca else None

        if req.dca_qty:
            # Strategy trade: DCA qty is fixed (multiplier × entry_qty).
            # Solve for entry_qty such that combined risk == risk_amount.
            # For LONG:  total_risk = Q*(E + M*D - (1+M)*SL)  → Q = risk / denom
            # For SHORT: total_risk = Q*((1+M)*SL - E - M*D)  → same formula, SL>entry
            if dca_price is None:
                raise ValidationError("dca_qty set but no dca_price — internal error.")
            M = req.dca_qty  # multiplier (2.0 for neil, 2.5 for saltwayer)
            if req.side == Side.LONG:
                denom = entry_price + M * dca_price - (1 + M) * sl_price
            else:
                denom = (1 + M) * sl_price - entry_price - M * dca_price
            if denom <= 0:
                raise ValidationError(
                    "Combined risk calculation failed — check that DCA price is between "
                    "entry and SL, and that SL is on the correct side."
                )
            raw_size = risk_amount / denom
            # Recalculate true risk_amount after rounding for display
            rounded = round(raw_size, qty_precision)
            dca_qty_abs = round(rounded * M, qty_precision)
            if req.side == Side.LONG:
                avg_entry = (rounded * entry_price + dca_qty_abs * dca_price) / (rounded + dca_qty_abs)
                risk_amount = (rounded + dca_qty_abs) * (avg_entry - sl_price)
            else:
                avg_entry = (rounded * entry_price + dca_qty_abs * dca_price) / (rounded + dca_qty_abs)
                risk_amount = (rounded + dca_qty_abs) * (sl_price - avg_entry)

        else:
            # No strategy multiplier — entry sized for full risk
            # Manual DCA (if any) is placed at same size as entry separately
            raw_size = risk_amount / stop_distance

        position_size = round(raw_size, qty_precision)
        if position_size < min_qty:
            raise ValidationError(
                f"Position size {position_size} {req.pair} is below the exchange minimum "
                f"of {min_qty}. Increase risk amount or widen your stop."
            )

        notional        = position_size * entry_price
        margin_required = notional / leverage

        if margin_required > available:
            raise InsufficientMarginError(
                f"Margin required ${margin_required:.2f} exceeds available ${available:.2f}. "
                f"Reduce risk amount or free up margin."
            )

        # Liquidation price estimate (isolated margin approximation)
        # Formula: liq = entry * (1 ∓ 1/lev + mmr)
        # where mmr = maintenance margin rate (~0.5% on Bybit linear)
        #
        # This matches Bybit's isolated-margin liquidation formula and gives
        # a directionally correct result for cross-margin too. The cross-margin
        # formula requires total account balance across all positions and is not
        # reliably calculable here — this is a close approximation.
        #
        # For LONG:  liq = entry * (1 - 1/lev + mmr)  → below entry
        # For SHORT: liq = entry * (1 + 1/lev - mmr)  → above entry
        mmr = 0.005   # Bybit linear maintenance margin rate ~0.5%
        if req.side == Side.LONG:
            liquidation_price = entry_price * (1 - 1 / leverage + mmr)
        else:
            liquidation_price = entry_price * (1 + 1 / leverage - mmr)
        liquidation_price = max(liquidation_price, 0.0)

        risk_percent = (risk_amount / equity) * 100

        return PositionSizing(
            balance=equity,          # show equity as the balance figure everywhere
            risk_amount=risk_amount,
            entry_price=entry_price,
            sl_price=sl_price,
            stop_distance=stop_distance,
            position_size=position_size,  # already rounded to exchange precision
            qty_precision=qty_precision,
            min_qty=min_qty,
            leverage=leverage,
            margin_required=round(margin_required, 2),
            liquidation_price=round(liquidation_price, 4),
            risk_percent=round(risk_percent, 2),
        )

    def validate_tps(self, req: TradeRequest, entry_price: float) -> None:
        """Validate that any set TPs are on the correct side of entry. Skip if tp is 0/None."""
        if req.side == Side.LONG:
            for name, tp in [("TP1", req.tp1), ("TP2", req.tp2), ("TP3", req.tp3)]:
                if tp and tp > 0 and tp <= entry_price:
                    raise ValidationError(f"{name} ({tp}) must be above entry ({entry_price}) for LONG.")
        else:
            for name, tp in [("TP1", req.tp1), ("TP2", req.tp2), ("TP3", req.tp3)]:
                if tp and tp > 0 and tp >= entry_price:
                    raise ValidationError(f"{name} ({tp}) must be below entry ({entry_price}) for SHORT.")