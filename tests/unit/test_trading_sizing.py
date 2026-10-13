from decimal import Decimal

from bybit_client.models import ResolvedInstrument
from screener.application.trading.sizing import (
    SizeReject,
    compute_qty,
    protective_prices,
)


def _inst(**kwargs) -> ResolvedInstrument:
    base = dict(
        symbol="BTCUSDT",
        status="Trading",
        contract_type="LinearPerpetual",
        base_coin="BTC",
        quote_coin="USDT",
        settle_coin="USDT",
        min_leverage=Decimal("1"),
        max_leverage=Decimal("50"),
        leverage_step=Decimal("0.5"),
        min_notional_value=Decimal("5"),
        min_order_qty=Decimal("0.001"),
        qty_step=Decimal("0.001"),
        max_mkt_order_qty=Decimal("100"),
        tick_size=Decimal("0.1"),
    )
    base.update(kwargs)
    return ResolvedInstrument(**base)


def test_round_down_qty_step() -> None:
    result = compute_qty(
        equity=Decimal("1000"),
        margin_pct=Decimal("2"),
        leverage=Decimal("3"),
        sizing_price=Decimal("100"),
        instrument=_inst(),
    )
    assert not isinstance(result, SizeReject)
    # notional = 1000*0.02*3 = 60; qty = 0.6 → floor to 0.001 step stays 0.6
    assert result.qty == Decimal("0.600")


def test_below_min_qty_rejects_without_bump() -> None:
    result = compute_qty(
        equity=Decimal("10"),
        margin_pct=Decimal("1"),
        leverage=Decimal("1"),
        sizing_price=Decimal("50000"),
        instrument=_inst(min_order_qty=Decimal("0.01"), qty_step=Decimal("0.001")),
    )
    assert isinstance(result, SizeReject)
    assert result.reason == "below_minimum_order"


def test_below_min_notional_rejects() -> None:
    result = compute_qty(
        equity=Decimal("10"),
        margin_pct=Decimal("1"),
        leverage=Decimal("1"),
        sizing_price=Decimal("100"),
        instrument=_inst(
            min_order_qty=Decimal("0.001"),
            qty_step=Decimal("0.001"),
            min_notional_value=Decimal("50"),
        ),
    )
    assert isinstance(result, SizeReject)


def test_protective_prices_long_and_short() -> None:
    tp, sl = protective_prices(
        side="Buy",
        avg_fill=Decimal("100"),
        take_profit_pct=Decimal("5"),
        stop_loss_pct=Decimal("3"),
        tick_size=Decimal("0.1"),
    )
    assert tp == Decimal("105.0")
    assert sl == Decimal("97.0")
    tp, sl = protective_prices(
        side="Sell",
        avg_fill=Decimal("100"),
        take_profit_pct=Decimal("5"),
        stop_loss_pct=Decimal("3"),
        tick_size=Decimal("0.1"),
    )
    assert tp == Decimal("95.0")
    assert sl == Decimal("103.0")
