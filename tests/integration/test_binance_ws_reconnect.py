"""Binance ticker stream reconnect and stale watchdog against a local fake server."""

from __future__ import annotations

import asyncio

import orjson
import pytest
import websockets

from screener.application.metrics import FeedMetrics
from screener.infrastructure.binance.ws import BinanceTickerStream


async def _wait_until(pred, timeout: float = 4.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if pred():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met")


@pytest.mark.asyncio
async def test_reconnects_after_server_close() -> None:
    connections = 0
    received: list[dict] = []
    connects: list[int] = []
    disconnects: list[int] = []

    async def server(ws) -> None:
        nonlocal connections
        connections += 1
        await ws.send(orjson.dumps({"stream": "!ticker@arr", "data": [{"s": "BTCUSDT", "E": 1}]}))
        if connections == 1:
            await ws.close()
            return
        await asyncio.sleep(2)

    async def on_message(msg: dict, _mono: float) -> None:
        received.append(msg)

    stats = FeedMetrics()
    async with websockets.serve(server, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        stream = BinanceTickerStream(
            f"ws://127.0.0.1:{port}",
            on_message=on_message,
            stats=stats,
            on_connect=lambda symbols: connects.append(len(symbols)),
            on_disconnect=lambda symbols, ts: disconnects.append(ts or 0),
            stale_connection_sec=30.0,
            compression=None,
        )
        await stream.start(["BTCUSDT"])
        try:
            await _wait_until(lambda: connections >= 2 and len(received) >= 2)
        finally:
            await stream.stop()

    assert connections >= 2
    assert stats.ws_reconnects >= 1
    assert disconnects
    assert stream.connection_count == 0


@pytest.mark.asyncio
async def test_stale_connection_reconnects() -> None:
    connections = 0

    async def server(ws) -> None:
        nonlocal connections
        connections += 1
        await ws.send(orjson.dumps({"stream": "!ticker@arr", "data": [{"s": "BTCUSDT", "E": 1}]}))
        await asyncio.sleep(5)

    stats = FeedMetrics()

    async def on_message(_msg: dict, _mono: float) -> None:
        return None

    async with websockets.serve(server, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        stream = BinanceTickerStream(
            f"ws://127.0.0.1:{port}",
            on_message=on_message,
            stats=stats,
            stale_connection_sec=0.4,
            compression=None,
        )
        await stream.start(["BTCUSDT"])
        try:
            await _wait_until(lambda: connections >= 2, timeout=6.0)
        finally:
            await stream.stop()

    assert connections >= 2
    assert stats.ws_reconnects >= 1
