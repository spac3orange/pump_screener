"""Bybit private WS client (fake local server) and raw → DTO mapping / routing."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from decimal import Decimal

import websockets

from bybit_client.private_ws import BybitPrivateStream, auth_payload
from screener.infrastructure.bybit.lifecycle_adapter import (
    PrivateMessageRouter,
    fill_from_bybit,
    position_from_bybit,
    protective_from_bybit,
)

EXECUTION_MSG = {
    "topic": "execution",
    "creationTime": 1760000000000,
    "data": [
        {
            "category": "linear",
            "symbol": "EPICUSDT",
            "execFee": "0.0181",
            "execId": "e-1",
            "execPrice": "0.3291",
            "execQty": "100",
            "execType": "Trade",
            "execValue": "32.91",
            "isMaker": False,
            "feeRate": "0.00055",
            "orderId": "o-1",
            "orderLinkId": "",
            "side": "Sell",
            "execTime": "1760000000000",
            "closedSize": "100",
            "execPnl": "1.89",
            "seq": 42,
            "stopOrderType": "TakeProfit",
            "createType": "CreateByTakeProfit",
        },
        {"category": "spot", "symbol": "EPICUSDT", "execId": "spot-1"},
    ],
}


def test_auth_signature_is_hmac_of_expires() -> None:
    payload = auth_payload("key", "secret", now=1000.0)
    _op, (key, expires, signature) = payload["op"], payload["args"]
    assert key == "key" and expires == 1_010_000
    expected = hmac.new(b"secret", b"GET/realtime1010000", hashlib.sha256).hexdigest()
    assert signature == expected


def test_fill_mapping() -> None:
    f = fill_from_bybit(EXECUTION_MSG["data"][0])
    assert f is not None
    assert (f.exec_id, f.order_id, f.side) == ("e-1", "o-1", "Sell")
    assert f.stop_order_type == "TakeProfit"
    assert f.price == Decimal("0.3291") and f.closed_size == Decimal("100")
    assert f.pnl == Decimal("1.89") and f.fee == Decimal("0.0181")
    assert f.time_ms == 1760000000000 and f.seq == 42


def test_position_and_order_mapping() -> None:
    state = position_from_bybit(
        {"symbol": "EPICUSDT", "size": "0", "side": "", "positionIdx": 0, "seq": 7}
    )
    assert state is not None and state.size == 0 and state.side is None
    assert position_from_bybit({"symbol": "X", "size": "1", "positionIdx": 1}) is None
    order = protective_from_bybit(
        {"orderId": "tp", "symbol": "X", "stopOrderType": "TakeProfit", "orderStatus": "New"}
    )
    assert order is not None and order.stop_order_type == "TakeProfit"
    assert protective_from_bybit({"orderId": "m", "stopOrderType": ""}) is None


class _Lifecycle:
    def __init__(self) -> None:
        self.fills: list = []
        self.positions: list = []
        self.orders: list = []

    async def on_fills(self, fills) -> None:
        self.fills += fills

    async def on_positions(self, states) -> None:
        self.positions += states

    async def on_protective_orders(self, orders) -> None:
        self.orders += orders


class _Gateway:
    def __init__(self) -> None:
        self.messages: list = []

    def handle_private_message(self, message) -> None:
        self.messages.append(message["topic"])


async def test_router_dispatches_linear_rows_and_fill_hints() -> None:
    lifecycle, gw = _Lifecycle(), _Gateway()
    router = PrivateMessageRouter(gw, lifecycle)  # type: ignore[arg-type]
    await router.handle(EXECUTION_MSG)
    await router.handle(
        {
            "topic": "position",
            "data": [{"category": "linear", "symbol": "EPICUSDT", "size": "0", "side": ""}],
        }
    )
    await router.handle(
        {
            "topic": "order",
            "data": [
                {
                    "category": "linear",
                    "symbol": "EPICUSDT",
                    "orderId": "sl",
                    "stopOrderType": "StopLoss",
                    "orderStatus": "Deactivated",
                }
            ],
        }
    )
    assert [f.exec_id for f in lifecycle.fills] == ["e-1"]  # spot row dropped
    assert len(lifecycle.positions) == 1 and lifecycle.positions[0].size == 0
    assert lifecycle.orders[0].status == "Deactivated"
    assert gw.messages == ["execution", "position", "order"]


async def test_stream_auth_subscribe_dispatch_and_reconnect() -> None:
    connections: list[list[dict]] = []

    async def server(ws) -> None:
        received: list[dict] = []
        connections.append(received)
        auth = json.loads(await ws.recv())
        received.append(auth)
        await ws.send(json.dumps({"op": "auth", "success": True, "ret_msg": ""}))
        sub = json.loads(await ws.recv())
        received.append(sub)
        await ws.send(json.dumps({"op": "subscribe", "success": True}))
        await ws.send(json.dumps(EXECUTION_MSG))
        if len(connections) == 1:
            await ws.close()  # force a reconnect
            return
        try:
            async for raw in ws:
                received.append(json.loads(raw))
        except websockets.ConnectionClosed:
            pass

    messages: list[dict] = []
    statuses: list[bool] = []

    async def on_message(message: dict) -> None:
        messages.append(message)

    async with websockets.serve(server, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        stream = BybitPrivateStream(
            api_key="k",
            api_secret="s",
            demo=True,
            on_message=on_message,
            on_status=statuses.append,
            ping_interval_sec=0.05,
            max_backoff_sec=0.1,
            url=f"ws://127.0.0.1:{port}",
        )
        task = asyncio.create_task(stream.run())
        try:
            for _ in range(100):
                if len(connections) >= 2 and any(
                    m.get("op") == "ping" for m in connections[1]
                ):
                    break
                await asyncio.sleep(0.05)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert len(connections) >= 2
    first = connections[0]
    assert first[0]["op"] == "auth" and first[0]["args"][0] == "k"
    assert first[1] == {"op": "subscribe", "args": ["order", "execution", "position"]}
    assert connections[1][1]["op"] == "subscribe"  # resubscribed after reconnect
    assert any(m.get("op") == "ping" for m in connections[1])
    assert len(messages) >= 2
    assert statuses[:3] == [True, False, True]
    assert stream.reconnects >= 1


async def test_stream_auth_failure_reports_disconnected() -> None:
    async def server(ws) -> None:
        await ws.recv()
        await ws.send(json.dumps({"op": "auth", "success": False, "ret_msg": "invalid"}))
        await asyncio.sleep(0.5)

    statuses: list[bool] = []

    async def on_message(message: dict) -> None:
        raise AssertionError("no data expected")

    async with websockets.serve(server, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        stream = BybitPrivateStream(
            api_key="k",
            api_secret="s",
            demo=True,
            on_message=on_message,
            on_status=statuses.append,
            max_backoff_sec=0.1,
            url=f"ws://127.0.0.1:{port}",
        )
        task = asyncio.create_task(stream.run())
        await asyncio.sleep(0.6)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert True not in statuses
    assert stream.reconnects >= 1
    assert not stream.connected
