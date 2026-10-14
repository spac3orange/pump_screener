"""classify_close: evidence from Bybit execution fields only; contradictions → UNKNOWN."""

from __future__ import annotations

import dataclasses

from screener.application.trading.close_classifier import classify_close
from screener.domain.trade_lifecycle import PositionCloseReason as R
from tests.unit.test_trade_lifecycle import fill, make_trade, sl_fill, tp_fill


def test_tp_by_stop_order_type() -> None:
    assert classify_close(make_trade(), [tp_fill()]).reason is R.TAKE_PROFIT


def test_sl_by_create_type_only() -> None:
    f = fill(create_type="CreateByStopLoss")
    assert classify_close(make_trade(), [f]).reason is R.STOP_LOSS


def test_tp_by_stored_order_id() -> None:
    trade = make_trade()
    trade.tp_order_id = "tp-9"
    assert classify_close(trade, [fill(order_id="tp-9")]).reason is R.TAKE_PROFIT


def test_conflicting_tp_and_sl_is_unknown() -> None:
    f = fill(stop_order_type="TakeProfit", create_type="CreateByStopLoss")
    result = classify_close(make_trade(), [f])
    assert result.reason is R.UNKNOWN
    assert "противоречив" in result.detail


def test_last_order_decides_after_partial_tp() -> None:
    trade = make_trade()
    partial = dataclasses.replace(
        tp_fill(order_id="a", qty="40", time_ms=trade.opened_ms + 10),
        stop_order_type="PartialTakeProfit",
    )
    final = fill(
        order_id="b", qty="60", create_type="CreateByClosing", time_ms=trade.opened_ms + 20
    )
    assert classify_close(trade, [final, partial]).reason is R.MANUAL


def test_entry_side_fills_are_not_closing() -> None:
    trade = make_trade()
    same_side = fill(side="Buy", stop_order_type="TakeProfit")
    assert classify_close(trade, [same_side]).reason is R.UNKNOWN


def test_fills_before_trade_opened_are_ignored() -> None:
    trade = make_trade()
    old = tp_fill(time_ms=trade.opened_ms - 60_000)
    assert classify_close(trade, [old]).reason is R.UNKNOWN


def test_adl() -> None:
    f = fill(exec_type="AdlTrade", create_type="CreateByAdl_PassThrough")
    assert classify_close(make_trade(), [f]).reason is R.ADL


def test_liquidation_wins_over_tp() -> None:
    fills = [tp_fill(qty="50"), fill(exec_type="BustTrade", qty="50")]
    assert classify_close(make_trade(), fills).reason is R.LIQUIDATION


def test_other_mechanism_is_external() -> None:
    f = fill(stop_order_type="TrailingStop", create_type="CreateByTrailingStop")
    result = classify_close(make_trade(), [f])
    assert result.reason is R.EXTERNAL_ORDER
    assert "TrailingStop" in result.detail


def test_own_link_id_is_unknown() -> None:
    f = fill(create_type="CreateByUser", order_link_id="link-1")
    assert classify_close(make_trade(), [f]).reason is R.UNKNOWN


def test_sl_close_identifiers() -> None:
    f = sl_fill(order_id="sl-7", exec_id="e-7")
    result = classify_close(make_trade(), [f])
    assert (result.close_order_id, result.close_execution_id) == ("sl-7", "e-7")
