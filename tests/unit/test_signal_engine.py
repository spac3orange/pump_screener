import pytest

from screener.application.clock import FakeClock
from screener.application.price_history import PriceHistoryManager
from screener.application.signal_engine import SignalEngine
from screener.config import ScreenerSettings
from screener.domain.enums import HistorySource, SignalType
from screener.domain.models import MarketTick


def _settings(**kwargs) -> ScreenerSettings:
    base = dict(
        enable_long=True,
        long_threshold_pct=40.0,
        long_interval_sec=720,
        enable_short=True,
        short_threshold_pct=10.0,
        short_interval_sec=1800,
        enable_dump=True,
        dump_threshold_pct=40.0,
        dump_interval_sec=900,
        enable_volume_spike=False,
        min_turnover_24h_usdt=0,
        warmup_suppress_until_live=False,
        cooldown_sec=300,
        max_tick_age_ms=60_000,
    )
    base.update(kwargs)
    return ScreenerSettings(**base)


def _engine(symbol: str, start_price: float, now_ms: int, **settings):
    clock = FakeClock(now_ms)
    hist = PriceHistoryManager()
    hist.seed_point(
        symbol,
        ts_ms=0,
        last_price=start_price,
        turnover_24h=1_000_000,
        source=HistorySource.TICKER,
    )
    hist._first_live_ts[symbol] = 0
    return SignalEngine(hist, _settings(**settings), clock=clock), clock


def _tick(symbol: str, price: float, now_ms: int) -> MarketTick:
    return MarketTick(
        symbol=symbol,
        last_price=price,
        turnover_24h=1_100_000,
        volume_24h=10,
        exchange_ts_ms=now_ms,
        received_ts_ms=now_ms,
    )


@pytest.mark.asyncio
async def test_small_pump_does_not_fire() -> None:
    engine, _clock = _engine("BTCUSDT", 100.0, 600_000)
    events = await engine.on_tick(_tick("BTCUSDT", 105.0, 600_000))
    assert events == []


@pytest.mark.asyncio
async def test_long_fires_at_40_percent_in_12_minutes() -> None:
    now = 720_000
    engine, _clock = _engine("BTCUSDT", 100.0, now)
    events = await engine.on_tick(_tick("BTCUSDT", 140.0, now))
    pumps = [event for event in events if event.signal_type == SignalType.PUMP]
    assert len(pumps) == 1
    assert pumps[0].interval_sec == 720
    assert pumps[0].change_pct == pytest.approx(40.0)


@pytest.mark.asyncio
async def test_short_fires_at_minus_10_percent_in_30_minutes() -> None:
    now = 1_800_000
    engine, _clock = _engine("ETHUSDT", 100.0, now, enable_long=False, enable_dump=False)
    events = await engine.on_tick(_tick("ETHUSDT", 90.0, now))
    assert len(events) == 1
    assert events[0].signal_type == SignalType.SHORT
    assert events[0].interval_sec == 1800


@pytest.mark.asyncio
async def test_dump_fires_at_minus_40_percent_in_15_minutes() -> None:
    now = 900_000
    engine, _clock = _engine("SOLUSDT", 100.0, now, enable_long=False, enable_short=False)
    events = await engine.on_tick(_tick("SOLUSDT", 60.0, now))
    assert len(events) == 1
    assert events[0].signal_type == SignalType.DUMP
    assert events[0].interval_sec == 900


@pytest.mark.asyncio
async def test_no_spam_on_small_increments() -> None:
    now = 720_000
    engine, clock = _engine("SOLUSDT", 100.0, now, enable_short=False, enable_dump=False)

    async def tick_at(price: float, ts: int):
        clock.set(ts)
        return await engine.on_tick(_tick("SOLUSDT", price, ts))

    first = await tick_at(140.0, now)
    assert len([event for event in first if event.signal_type == SignalType.PUMP]) == 1
    second = await tick_at(140.2, now + 1_000)
    assert [event for event in second if event.signal_type == SignalType.PUMP] == []
    third = await tick_at(140.5, now + 2_000)
    assert [event for event in third if event.signal_type == SignalType.PUMP] == []
