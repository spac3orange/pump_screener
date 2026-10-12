"""Lightweight reconnect state test without real Bybit."""

import asyncio

import pytest

from screener.infrastructure.bybit.ws import WebSocketManager


@pytest.mark.asyncio
async def test_manager_stop_cleans_shards() -> None:
    received: list[dict] = []

    async def on_message(msg: dict) -> None:
        received.append(msg)

    mgr = WebSocketManager(
        "wss://127.0.0.1:1",  # will fail to connect
        on_message=on_message,
        symbols_per_connection=10,
        stale_connection_sec=5,
    )
    await mgr.start(["BTCUSDT", "ETHUSDT"])
    await asyncio.sleep(0.2)
    await mgr.stop()
    assert mgr.connection_count == 0
