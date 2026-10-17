"""Bitget ticker stream: ping, reconnect, and resubscribe against a local fake server."""

from __future__ import annotations

import asyncio

import orjson
import pytest
import websockets

from screener.application.metrics import FeedMetrics
from screener.infrastructure.bitget.ws import BitgetTickerStream


async def _wait_until(pred, timeout: float = 4.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if pred():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met")


def _ticker() -> bytes:
    return orjson.dumps(
        {
            "action": "snapshot",
            "arg": {"instType": "usdt-futures", "topic": "ticker", "symbol": "BTCUSDT"},
            "data": [{"lastPrice": "1"}],
            "ts": 1,
        }
    )


@pytest.mark.asyncio
async def test_ping_and_reconnect_resubscribes() -> None:
    connections = 0
    pings = 0
    subscribes: list[list[str]] = []
    received: list[dict] = []

    async def server(ws) -> None:
        nonlocal connections, pings
        connections += 1
        while True:
            raw = await ws.recv()
            if raw == "ping":
                pings += 1
                await ws.send("pong")
                continue
            msg = orjson.loads(raw)
            if msg.get("op") == "subscribe":
                subscribes.append([arg["symbol"] for arg in msg["args"]])
                await ws.send(_ticker())
                if connections == 1:
                    await ws.close()
                    return

    async def on_message(msg: dict, _mono: float) -> None:
        received.append(msg)

    stats = FeedMetrics()
    async with websockets.serve(server, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        stream = BitgetTickerStream(
            f"ws://127.0.0.1:{port}",
            on_message=on_message,
            stats=stats,
            stale_connection_sec=30.0,
            ping_interval_sec=0.2,
            compression=None,
        )
        await stream.start(["BTCUSDT", "ETHUSDT"])
        try:
            await _wait_until(lambda: connections >= 2 and len(subscribes) >= 2 and pings >= 1)
            await _wait_until(lambda: len(received) >= 2)
        finally:
            await stream.stop()

    assert stats.ws_reconnects >= 1
    assert all(set(batch) == {"BTCUSDT", "ETHUSDT"} for batch in subscribes[:2])
    assert stream.connection_count == 0


@pytest.mark.asyncio
async def test_stale_connection_reconnects() -> None:
    connections = 0

    async def server(ws) -> None:
        nonlocal connections
        connections += 1
        raw = await ws.recv()
        assert orjson.loads(raw)["op"] == "subscribe"
        await ws.send(_ticker())
        await asyncio.sleep(5)

    stats = FeedMetrics()

    async def on_message(_msg: dict, _mono: float) -> None:
        return None

    async with websockets.serve(server, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        stream = BitgetTickerStream(
            f"ws://127.0.0.1:{port}",
            on_message=on_message,
            stats=stats,
            stale_connection_sec=0.4,
            ping_interval_sec=10.0,
            compression=None,
        )
        await stream.start(["BTCUSDT"])
        try:
            await _wait_until(lambda: connections >= 2 and stats.ws_reconnects >= 1)
        finally:
            await stream.stop()
