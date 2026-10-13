from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from bybit_client.models import ResolvedInstrument


@dataclass(frozen=True, slots=True)
class SizeResult:
    qty: Decimal
    notional: Decimal
    target_margin: Decimal
    sizing_price: Decimal


@dataclass(frozen=True, slots=True)
class SizeReject:
    reason: str


def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    units = (value / step).to_integral_value(rounding=ROUND_DOWN)
    return units * step


def round_price_to_tick(price: Decimal, tick: Decimal, *, up: bool) -> Decimal:
    if tick <= 0:
        return price
    units = price / tick
    if up:
        from decimal import ROUND_UP

        return units.to_integral_value(rounding=ROUND_UP) * tick
    return units.to_integral_value(rounding=ROUND_DOWN) * tick


def compute_qty(
    *,
    equity: Decimal,
    margin_pct: Decimal,
    leverage: Decimal,
    sizing_price: Decimal,
    instrument: ResolvedInstrument,
) -> SizeResult | SizeReject:
    if equity <= 0 or sizing_price <= 0 or leverage <= 0:
        return SizeReject("invalid_sizing_inputs")
    target_margin = equity * (margin_pct / Decimal("100"))
    notional = target_margin * leverage
    raw_qty = notional / sizing_price
    qty = _floor_to_step(raw_qty, instrument.qty_step)
    if qty <= 0:
        return SizeReject("below_minimum_order")
    if qty < instrument.min_order_qty:
        return SizeReject("below_minimum_order")
    if instrument.max_mkt_order_qty > 0 and qty > instrument.max_mkt_order_qty:
        qty = _floor_to_step(instrument.max_mkt_order_qty, instrument.qty_step)
    final_notional = qty * sizing_price
    if instrument.min_notional_value > 0 and final_notional < instrument.min_notional_value:
        return SizeReject("below_minimum_order")
    return SizeResult(
        qty=qty,
        notional=final_notional,
        target_margin=target_margin,
        sizing_price=sizing_price,
    )


def clamp_leverage(desired: Decimal, instrument: ResolvedInstrument) -> Decimal:
    lev = min(desired, instrument.max_leverage)
    lev = max(lev, instrument.min_leverage)
    if instrument.leverage_step > 0:
        lev = _floor_to_step(lev, instrument.leverage_step)
        lev = max(lev, instrument.min_leverage)
    return lev


def protective_prices(
    *,
    side: str,
    avg_fill: Decimal,
    take_profit_pct: Decimal | None,
    stop_loss_pct: Decimal | None,
    tick_size: Decimal,
) -> tuple[Decimal | None, Decimal | None]:
    """Return (take_profit, stop_loss) rounded to tick from avg fill."""
    is_long = side.lower() == "buy"
    tp = sl = None
    if take_profit_pct is not None and take_profit_pct > 0:
        raw = (
            avg_fill * (Decimal("1") + take_profit_pct / Decimal("100"))
            if is_long
            else avg_fill * (Decimal("1") - take_profit_pct / Decimal("100"))
        )
        tp = round_price_to_tick(raw, tick_size, up=is_long)
    if stop_loss_pct is not None and stop_loss_pct > 0:
        raw = (
            avg_fill * (Decimal("1") - stop_loss_pct / Decimal("100"))
            if is_long
            else avg_fill * (Decimal("1") + stop_loss_pct / Decimal("100"))
        )
        sl = round_price_to_tick(raw, tick_size, up=not is_long)
    return tp, sl
