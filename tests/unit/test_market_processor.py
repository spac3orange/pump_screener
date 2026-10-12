import pytest

from screener.application.clock import FakeClock
from screener.application.market_processor import MarketDataProcessor
from screener.application.price_history import PriceHistoryManager


@pytest.mark.asyncio
async def test_snapshot_then_delta() -> None:
    clock = FakeClock(1_000_000)
    hist = PriceHistoryManager()
    proc = MarketDataProcessor(hist, max_tick_age_ms=10_000, clock=clock)

    snap = {
        "topic": "tickers.BTCUSDT",
        "type": "snapshot",
        "ts": 1_000_000,
        "cs": 1,
        "data": {
            "symbol": "BTCUSDT",
            "lastPrice": "100",
            "turnover24h": "1000",
            "volume24h": "10",
        },
    }
    tick = await proc.handle_ws_message(snap)
    assert tick is not None
    assert tick.last_price == 100.0

    delta = {
        "topic": "tickers.BTCUSDT",
        "type": "delta",
        "ts": 1_000_100,
        "cs": 2,
        "data": {"symbol": "BTCUSDT", "lastPrice": "101"},
    }
    clock.set(1_000_100)
    tick2 = await proc.handle_ws_message(delta)
    assert tick2 is not None
    assert tick2.last_price == 101.0
    assert tick2.turnover_24h == 1000.0


@pytest.mark.asyncio
async def test_delta_before_snapshot_ignored() -> None:
    clock = FakeClock(1_000_000)
    proc = MarketDataProcessor(PriceHistoryManager(), max_tick_age_ms=10_000, clock=clock)
    delta = {
        "topic": "tickers.ETHUSDT",
        "type": "delta",
        "ts": 1_000_000,
        "cs": 1,
        "data": {"symbol": "ETHUSDT", "lastPrice": "200"},
    }
    assert await proc.handle_ws_message(delta) is None


@pytest.mark.asyncio
async def test_stale_message_rejected() -> None:
    clock = FakeClock(1_000_000)
    proc = MarketDataProcessor(PriceHistoryManager(), max_tick_age_ms=1000, clock=clock)
    snap = {
        "topic": "tickers.SOLUSDT",
        "type": "snapshot",
        "ts": 990_000,  # 10s old
        "cs": 1,
        "data": {"symbol": "SOLUSDT", "lastPrice": "10", "turnover24h": "1", "volume24h": "1"},
    }
    assert await proc.handle_ws_message(snap) is None
