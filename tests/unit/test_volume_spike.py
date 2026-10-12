import pytest

from screener.application.clock import FakeClock
from screener.application.price_history import PriceHistoryManager
from screener.application.signal_engine import SignalEngine
from screener.config import ScreenerSettings
from screener.domain.enums import HistorySource, SignalType
from screener.domain.models import MarketTick


@pytest.mark.asyncio
async def test_volume_spike_does_not_emit_on_flat_price() -> None:
    clock = FakeClock(600_000)
    hist = PriceHistoryManager()
    for i in range(0, 11):
        hist.seed_point(
            "AAAUSDT",
            ts_ms=i * 60_000,
            last_price=1.0,
            turnover_24h=float(i * 1000),
            source=HistorySource.TICKER,
        )
    hist._first_live_ts["AAAUSDT"] = 0
    engine = SignalEngine(
        hist,
        ScreenerSettings(
            enable_volume_spike=True,
            min_turnover_24h_usdt=0,
            warmup_suppress_until_live=False,
            max_tick_age_ms=60_000,
        ),
        clock=clock,
    )
    events = await engine.on_tick(
        MarketTick(
            symbol="AAAUSDT",
            last_price=1.0,
            turnover_24h=14_000.0,
            volume_24h=1.0,
            exchange_ts_ms=600_000,
            received_ts_ms=600_000,
        )
    )
    assert all(event.signal_type != SignalType.VOLUME_SPIKE for event in events)
