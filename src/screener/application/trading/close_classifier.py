"""Why did a position close? Decided only from Bybit execution fields, never from prices.

The order that brought the position to zero is the *last* closing execution. Its
``stopOrderType`` / ``createType`` / ``execType`` (Bybit V5 enums) name the mechanism.
A cancelled TP/SL leg is not evidence of anything: only executions are.
Contradictory or missing evidence yields ``UNKNOWN`` rather than a guess.
"""

from __future__ import annotations

from collections.abc import Iterable

from screener.domain.trade_lifecycle import (
    CloseClassification,
    Fill,
    OpenTrade,
    PositionCloseReason,
)

_TP_STOP_TYPES = frozenset({"TakeProfit", "PartialTakeProfit"})
_SL_STOP_TYPES = frozenset({"StopLoss", "PartialStopLoss"})
_TP_CREATE_TYPES = frozenset({"CreateByTakeProfit", "CreateByPartialTakeProfit"})
_SL_CREATE_TYPES = frozenset({"CreateByStopLoss", "CreateByPartialStopLoss"})
_LIQ_CREATE_TYPES = frozenset({"CreateByLiq", "CreateByTakeOver_PassThrough"})
_ADL_CREATE_TYPES = frozenset({"CreateByAdl_PassThrough"})
_MANUAL_CREATE_TYPES = frozenset({"CreateByClosing"})
_EMPTY = frozenset({"", "UNKNOWN"})

_DETAIL = {
    PositionCloseReason.TAKE_PROFIT: "сработал Take Profit",
    PositionCloseReason.STOP_LOSS: "сработал Stop Loss",
    PositionCloseReason.MANUAL: "ручное закрытие в Bybit (веб/приложение)",
    PositionCloseReason.LIQUIDATION: "ликвидация",
    PositionCloseReason.ADL: "авто-делевередж (ADL) биржи",
}


def _values(fills: Iterable[Fill], attr: str) -> set[str]:
    return {getattr(f, attr) for f in fills} - _EMPTY


def _result(
    reason: PositionCloseReason, fill: Fill | None, detail: str | None = None
) -> CloseClassification:
    return CloseClassification(
        reason=reason,
        detail=detail or _DETAIL.get(reason, ""),
        close_order_id=fill.order_id if fill else None,
        close_execution_id=fill.exec_id if fill else None,
    )


def classify_close(trade: OpenTrade, fills: Iterable[Fill]) -> CloseClassification:
    closing = sorted(
        (f for f in fills if trade.is_closing_fill(f)), key=lambda f: (f.time_ms, f.seq)
    )
    if not closing:
        return _result(PositionCloseReason.UNKNOWN, None, "нет данных об исполнении закрытия")

    for fill in closing:
        if fill.exec_type == "BustTrade" or fill.create_type in _LIQ_CREATE_TYPES:
            return _result(PositionCloseReason.LIQUIDATION, fill)
    for fill in closing:
        if fill.exec_type == "AdlTrade" or fill.create_type in _ADL_CREATE_TYPES:
            return _result(PositionCloseReason.ADL, fill)

    last = closing[-1]
    final = [f for f in closing if f.order_id == last.order_id]
    stop_types = _values(final, "stop_order_type")
    create_types = _values(final, "create_type")

    tp = bool(stop_types & _TP_STOP_TYPES or create_types & _TP_CREATE_TYPES)
    sl = bool(stop_types & _SL_STOP_TYPES or create_types & _SL_CREATE_TYPES)
    if trade.tp_order_id and last.order_id == trade.tp_order_id:
        tp = True
    if trade.sl_order_id and last.order_id == trade.sl_order_id:
        sl = True
    if tp and sl:
        return _result(PositionCloseReason.UNKNOWN, last, "противоречивые данные TP/SL")
    if tp:
        return _result(PositionCloseReason.TAKE_PROFIT, last)
    if sl:
        return _result(PositionCloseReason.STOP_LOSS, last)

    if create_types & _MANUAL_CREATE_TYPES:
        return _result(PositionCloseReason.MANUAL, last)
    if last.order_link_id and last.order_link_id == trade.order_link_id:
        return _result(PositionCloseReason.UNKNOWN, last, "закрытие ордером входа")
    if create_types == {"CreateByUser"} and not stop_types:
        return _result(
            PositionCloseReason.EXTERNAL_ORDER, last, "ордер вне PumpScreener (API/другой бот)"
        )
    mechanism = sorted(stop_types | create_types)
    if mechanism:
        return _result(
            PositionCloseReason.EXTERNAL_ORDER, last, f"внешний ордер ({', '.join(mechanism)})"
        )
    return _result(PositionCloseReason.UNKNOWN, last, "тип закрывающего ордера не указан")
