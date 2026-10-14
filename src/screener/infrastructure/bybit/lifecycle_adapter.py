"""Bybit V5 private data → SDK-free lifecycle DTOs (the only place that reads raw fields)."""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from screener.domain.trade_lifecycle import (
    ClosedPnlRecord,
    Fill,
    PositionState,
    ProtectiveOrder,
)

if TYPE_CHECKING:
    from bybit_client import BybitGateway
    from screener.application.trading.lifecycle import PositionLifecycleService

logger = logging.getLogger(__name__)

_CATEGORY = "linear"
# /v5/execution/list and /v5/position/closed-pnl accept at most 7 days per query.
_MAX_QUERY_SPAN_MS = 7 * 24 * 3600 * 1000 - 1000
_PROTECTIVE_STOP_TYPES = frozenset({"TakeProfit", "StopLoss"})


def _dec(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _dec0(value: object) -> Decimal:
    return _dec(value) or Decimal("0")


def _int(value: object) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def fill_from_bybit(row: dict[str, Any]) -> Fill | None:
    exec_id = str(row.get("execId") or "")
    symbol = str(row.get("symbol") or "")
    if not exec_id or not symbol:
        return None
    return Fill(
        exec_id=exec_id,
        symbol=symbol,
        order_id=str(row.get("orderId") or ""),
        order_link_id=str(row.get("orderLinkId") or ""),
        side=str(row.get("side") or ""),
        exec_type=str(row.get("execType") or ""),
        create_type=str(row.get("createType") or ""),
        stop_order_type=str(row.get("stopOrderType") or ""),
        price=_dec0(row.get("execPrice")),
        qty=_dec0(row.get("execQty")),
        closed_size=_dec0(row.get("closedSize")),
        fee=_dec0(row.get("execFee")),
        pnl=_dec(row.get("execPnl")),
        time_ms=_int(row.get("execTime")),
        seq=_int(row.get("seq")),
    )


def position_from_bybit(row: dict[str, Any]) -> PositionState | None:
    symbol = str(row.get("symbol") or "")
    if not symbol or _int(row.get("positionIdx")) != 0:
        return None
    return PositionState(
        symbol=symbol,
        size=_dec0(row.get("size")),
        side=str(row.get("side") or "") or None,
        status=str(row.get("positionStatus") or ""),
        seq=_int(row.get("seq")),
        updated_ms=_int(row.get("updatedTime")),
    )


def protective_from_bybit(row: dict[str, Any]) -> ProtectiveOrder | None:
    stop_type = str(row.get("stopOrderType") or "")
    if stop_type not in _PROTECTIVE_STOP_TYPES:
        return None
    return ProtectiveOrder(
        order_id=str(row.get("orderId") or ""),
        symbol=str(row.get("symbol") or ""),
        stop_order_type=stop_type,
        status=str(row.get("orderStatus") or ""),
        cancel_type=str(row.get("cancelType") or ""),
    )


def closed_pnl_from_bybit(row: dict[str, Any]) -> ClosedPnlRecord | None:
    order_id = str(row.get("orderId") or "")
    pnl = _dec(row.get("closedPnl"))
    if not order_id or pnl is None:
        return None
    return ClosedPnlRecord(
        order_id=order_id,
        symbol=str(row.get("symbol") or ""),
        closed_pnl=pnl,
        avg_exit_price=_dec(row.get("avgExitPrice")),
        closed_size=_dec0(row.get("closedSize")),
        updated_ms=_int(row.get("updatedTime")),
        open_fee=_dec(row.get("openFee")),
        close_fee=_dec(row.get("closeFee")),
    )


def _rows(message: dict[str, Any]) -> list[dict[str, Any]]:
    data = message.get("data") or []
    if isinstance(data, dict):
        data = [data]
    return [
        row
        for row in data
        if isinstance(row, dict) and row.get("category") in (None, _CATEGORY)
    ]


def _span(start_ms: int, end_ms: int) -> tuple[int, int]:
    return max(start_ms, end_ms - _MAX_QUERY_SPAN_MS), end_ms


class BybitLifecycleGateway:
    """``LifecycleGateway`` port over ``BybitGateway`` REST."""

    def __init__(self, gateway: BybitGateway) -> None:
        self._gw = gateway

    async def get_open_positions(self) -> dict[str, PositionState]:
        out: dict[str, PositionState] = {}
        for row in await self._gw.list_position_rows():
            state = position_from_bybit(row)
            if state is not None and state.size > 0:
                out[state.symbol] = state
        return out

    async def get_position(self, symbol: str) -> PositionState:
        snap = await self._gw.get_position(symbol)
        return PositionState(symbol=symbol, size=snap.size, side=snap.side)

    async def get_fills(self, symbol: str, start_ms: int, end_ms: int) -> list[Fill]:
        start, end = _span(start_ms, end_ms)
        rows = await self._gw.get_executions(symbol=symbol, start_ms=start, end_ms=end)
        return [f for f in map(fill_from_bybit, rows) if f is not None]

    async def get_order_fills(self, symbol: str, order_id: str) -> list[Fill]:
        rows = await self._gw.get_executions(symbol=symbol, order_id=order_id)
        return [f for f in map(fill_from_bybit, rows) if f is not None]

    async def get_closed_pnl(
        self, symbol: str, start_ms: int, end_ms: int
    ) -> list[ClosedPnlRecord]:
        start, end = _span(start_ms, end_ms)
        rows = await self._gw.get_closed_pnl(symbol=symbol, start_ms=start, end_ms=end)
        return [r for r in map(closed_pnl_from_bybit, rows) if r is not None]

    async def get_protective_orders(self, symbol: str) -> list[ProtectiveOrder]:
        rows = await self._gw.list_open_orders(symbol=symbol)
        return [o for o in map(protective_from_bybit, rows) if o is not None]

    async def get_equity(self) -> Decimal:
        return (await self._gw.get_balance()).total_equity


class PrivateMessageRouter:
    """Private WS message → fill hints for entry execution + lifecycle service."""

    def __init__(
        self, gateway: BybitGateway | None, lifecycle: PositionLifecycleService
    ) -> None:
        self._gw = gateway
        self._lifecycle = lifecycle

    async def handle(self, message: dict[str, Any]) -> None:
        topic = str(message.get("topic") or "")
        if self._gw is not None:
            try:
                self._gw.handle_private_message(message)
            except Exception:
                logger.warning("Fill hint handling failed", exc_info=True)
        rows = _rows(message)
        if topic.startswith("execution"):
            fills = [f for f in map(fill_from_bybit, rows) if f is not None]
            if fills:
                await self._lifecycle.on_fills(fills)
        elif topic.startswith("position"):
            states = [s for s in map(position_from_bybit, rows) if s is not None]
            if states:
                await self._lifecycle.on_positions(states)
        elif topic.startswith("order"):
            orders = [o for o in map(protective_from_bybit, rows) if o is not None]
            if orders:
                await self._lifecycle.on_protective_orders(orders)
