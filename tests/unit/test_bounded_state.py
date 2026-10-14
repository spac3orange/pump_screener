from __future__ import annotations

from datetime import UTC, datetime, timedelta

from screener.application.dedup import DedupKey, SignalDeduplicator
from screener.application.notification_service import RecentSignals
from screener.domain.enums import SignalType


def _trigger(dedup: SignalDeduplicator, symbol: str, interval: int = 600) -> None:
    dedup.evaluate(
        DedupKey(symbol, SignalType.PUMP, interval),
        change_pct=50,
        threshold_pct=40,
        now_ms=1,
        triggered=True,
    )


def test_dedup_clear_symbol_only_drops_that_symbol() -> None:
    dedup = SignalDeduplicator()
    _trigger(dedup, "AAAUSDT", 600)
    _trigger(dedup, "AAAUSDT", 900)
    _trigger(dedup, "BBBUSDT")
    assert dedup.clear_symbol("AAAUSDT") == 2
    assert dedup.active_count == 1
    dedup.clear()
    assert dedup.active_count == 0


def test_recent_signals_drop_expired_symbols() -> None:
    recent = RecentSignals()
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    recent.record("OLDUSDT", t0)
    assert recent.count("OLDUSDT", t0 + timedelta(hours=25)) == 0
    assert len(recent) == 0


def test_recent_signals_periodic_sweep_bounds_keys() -> None:
    recent = RecentSignals()
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(RecentSignals._SWEEP_EVERY - 1):
        recent.record(f"S{i}USDT", t0)
    later = t0 + timedelta(hours=25)
    recent.record("NEWUSDT", later)
    assert len(recent) == 1
    recent.forget("NEWUSDT")
    assert len(recent) == 0
