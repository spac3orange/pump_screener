from __future__ import annotations

import httpx
import pytest

from screener.application.metrics import FeedMetrics
from screener.infrastructure.binance.rest import (
    WEIGHT_SOFT_LIMIT_1M,
    BinanceRateLimited,
    BinanceRestClient,
)


def _client(handler, stats: FeedMetrics | None = None) -> BinanceRestClient:
    transport = httpx.MockTransport(handler)
    return BinanceRestClient("https://fapi.binance.com", stats=stats, transport=transport)


@pytest.mark.asyncio
async def test_fetch_instruments_reads_symbols() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/fapi/v1/exchangeInfo"
        return httpx.Response(
            200,
            json={"symbols": [{"symbol": "BTCUSDT"}, {"symbol": "ETHUSDT"}]},
            headers={"X-MBX-USED-WEIGHT-1M": "1"},
        )

    client = _client(handler)
    try:
        items = await client.fetch_instruments()
        assert [i["symbol"] for i in items] == ["BTCUSDT", "ETHUSDT"]
        assert client.used_weight_1m == 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_fetch_kline_maps_interval_and_returns_rows() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/fapi/v1/klines"
        assert request.url.params["interval"] == "1m"
        assert request.url.params["limit"] == "10"
        return httpx.Response(
            200,
            json=[["1700000040000", "1", "1", "1", "1.5", "1"]],
            headers={"X-MBX-USED-WEIGHT-1M": "1"},
        )

    client = _client(handler)
    try:
        rows = await client.fetch_kline("BTCUSDT", interval="1", limit=10)
        assert rows[0][4] == "1.5"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_http_429_pauses_further_requests() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "120", "X-MBX-USED-WEIGHT-1M": "2400"})

    stats = FeedMetrics()
    client = _client(handler, stats)
    try:
        with pytest.raises(BinanceRateLimited):
            await client.fetch_instruments()
        assert stats.rest_throttled_total >= 1
        assert client.paused_for_sec >= 100
        with pytest.raises(BinanceRateLimited):
            await client.fetch_instruments()
        assert calls == 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_soft_weight_limit_pauses() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"symbols": []},
            headers={"X-MBX-USED-WEIGHT-1M": str(WEIGHT_SOFT_LIMIT_1M)},
        )

    stats = FeedMetrics()
    client = _client(handler, stats)
    try:
        await client.fetch_instruments()
        assert stats.rest_throttled_total == 1
        assert client.paused_for_sec > 0
    finally:
        await client.aclose()
