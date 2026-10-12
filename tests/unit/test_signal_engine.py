import pytest

from screener.application.clock import FakeClock
from screener.application.price_history import PriceHistoryManager
from screener.application.signal_engine import SignalEngine
from screener.config import ScreenerSettings
from screener.domain.enums import HistorySource, SignalType
from screener.domain.models import MarketTick


def _settings(**kwargs) -> ScreenerSettings:
    base = dict(
        enable_pump=True,
        enable_dump=True,
        enable_volume_spike=False,
        signal_intervals_sec=[600],
        pump_threshold_pct=5.0,
        dump_threshold_pct=5.0,
        min_turnover_24h_usdt=0,
        warmup_suppress_until_live=False,
        cooldown_sec=300,
        max_tick_age_ms=60_000,
    )
    base.update(kwargs)
    return ScreenerSettings(**base)


@pytest.mark.asyncio
async def test_detects_pump() -> None:
    clock = FakeClock(600_000)
    hist = PriceHistoryManager()
    # 10 minutes ago price 100
    hist.seed_point(
        "BTCUSDT",
        ts_ms=0,
        last_price=100.0,
        turnover_24h=1_000_000,
        source=HistorySource.TICKER,
    )
    hist._first_live_ts["BTCUSDT"] = 0
    engine = SignalEngine(hist, _settings(), clock=clock)
    tick = MarketTick(
        symbol="BTCUSDT",
        last_price=105.0,
        turnover_24h=1_100_000,
        volume_24h=10,
        exchange_ts_ms=600_000,
        received_ts_ms=600_000,
    )
    events = await engine.on_tick(tick)
    types = {e.signal_type for e in events}
    assert SignalType.PUMP in types


@pytest.mark.asyncio
async def test_detects_dump() -> None:
    clock = FakeClock(600_000)
    hist = PriceHistoryManager()
    hist.seed_point(
        "ETHUSDT",
        ts_ms=0,
        last_price=100.0,
        turnover_24h=1_000_000,
        source=HistorySource.TICKER,
    )
    hist._first_live_ts["ETHUSDT"] = 0
    engine = SignalEngine(hist, _settings(), clock=clock)
    tick = MarketTick(
        symbol="ETHUSDT",
        last_price=94.0,
        turnover_24h=1_100_000,
        volume_24h=10,
        exchange_ts_ms=600_000,
        received_ts_ms=600_000,
    )
    events = await engine.on_tick(tick)
    assert any(e.signal_type == SignalType.DUMP for e in events)


@pytest.mark.asyncio
async def test_no_spam_on_small_increments() -> None:
    clock = FakeClock(600_000)
    hist = PriceHistoryManager()
    hist.seed_point(
        "SOLUSDT",
        ts_ms=0,
        last_price=100.0,
        turnover_24h=1_000_000,
        source=HistorySource.TICKER,
    )
    hist._first_live_ts["SOLUSDT"] = 0
    engine = SignalEngine(hist, _settings(cooldown_sec=300), clock=clock)

    async def tick_at(price: float, now: int):
        clock.set(now)
        # keep past point valid relative to now
        return await engine.on_tick(
            MarketTick(
                symbol="SOLUSDT",
                last_price=price,
                turnover_24h=1_100_000,
                volume_24h=10,
                exchange_ts_ms=now,
                received_ts_ms=now,
            )
        )

    e1 = await tick_at(105.0, 600_000)
    assert len([x for x in e1 if x.signal_type == SignalType.PUMP]) == 1
    e2 = await tick_at(105.1, 601_000)
    assert len([x for x in e2 if x.signal_type == SignalType.PUMP]) == 0
    e3 = await tick_at(105.3, 602_000)
    assert len([x for x in e3 if x.signal_type == SignalType.PUMP]) == 0
