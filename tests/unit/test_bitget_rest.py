from __future__ import annotations

import httpx
import pytest

from screener.application.metrics import FeedMetrics
from screener.infrastructure.bitget.rest import (
    BitgetApiError,
    BitgetRateLimited,
    BitgetRestClient,
)


def _client(handler, stats: FeedMetrics | None = None) -> BitgetRestClient:
    transport = httpx.MockTransport(handler)
    return BitgetRestClient("https://api.bitget.com", stats=stats, transport=transport)


@pytest.mark.asyncio
async def test_fetch_instruments_requires_code_00000() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/market/instruments"
        assert request.url.params["category"] == "USDT-FUTURES"
        return httpx.Response(200, json={"code": "00000", "data": [{"symbol": "BTCUSDT"}]})

    client = _client(handler)
    try:
        items = await client.fetch_instruments()
        assert items == [{"symbol": "BTCUSDT"}]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_non_success_code_is_an_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": "40001", "msg": "no", "data": []})

    client = _client(handler)
    try:
        with pytest.raises(BitgetApiError):
            await client.fetch_instruments()
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_fetch_kline_maps_interval() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/market/candles"
        assert request.url.params["interval"] == "1m"
        assert request.url.params["symbol"] == "BTCUSDT"
        assert request.url.params["limit"] == "10"
        return httpx.Response(
            200,
            json={"code": "00000", "data": [["1700000040000", "1", "1", "1", "1.5", "1"]]},
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
        return httpx.Response(429, headers={"Retry-After": "120"})

    stats = FeedMetrics()
    client = _client(handler, stats)
    try:
        with pytest.raises(BitgetRateLimited):
            await client.fetch_instruments()
        assert stats.rest_throttled_total == 1
        assert client.paused_for_sec >= 100
        with pytest.raises(BitgetRateLimited):
            await client.fetch_instruments()
        assert calls == 1
    finally:
        await client.aclose()
