import pytest

from screener.application.clock import FakeClock
from screener.application.price_history import PriceHistoryManager
from screener.application.signal_engine import SignalEngine
from screener.config import ScreenerSettings
from screener.domain.enums import HistorySource, SignalType
from screener.domain.models import MarketTick


@pytest.mark.asyncio
async def test_volume_spike_detection() -> None:
    clock = FakeClock(600_000)
    hist = PriceHistoryManager()
    # Build baseline windows of ~1000 turnover delta each, then spike 4000
    # Points every 60s from t=0..600000 with rising turnover24h
    # For interval 60: deltas between consecutive points
    for i in range(0, 11):
        ts = i * 60_000
        # steady +1000 per minute for first 9 minutes, then +5000
        turnover = i * 1000 if i < 10 else 9_000 + 5_000
        hist.seed_point(
            "AAAUSDT",
            ts_ms=ts,
            last_price=1.0,
            turnover_24h=float(turnover),
            source=HistorySource.TICKER,
        )
    hist._first_live_ts["AAAUSDT"] = 0

    settings = ScreenerSettings(
        enable_pump=False,
        enable_dump=False,
        enable_volume_spike=True,
        signal_intervals_sec=[60],
        volume_ratio_threshold=3.0,
        min_turnover_24h_usdt=0,
        min_abs_turnover_window_usdt=1000,
        volume_baseline_windows=6,
        warmup_suppress_until_live=False,
        cooldown_sec=0,
        max_tick_age_ms=60_000,
    )
    engine = SignalEngine(hist, settings, clock=clock)
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
    assert any(e.signal_type == SignalType.VOLUME_SPIKE for e in events)
