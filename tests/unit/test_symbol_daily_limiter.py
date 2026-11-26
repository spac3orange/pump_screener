from datetime import UTC, datetime

from screener.application.symbol_daily_limiter import SymbolDailyLimiter
from screener.app_state import AppState
from screener.config import ScreenerSettings
from screener.domain.enums import SignalType
from tests.helpers import make_signal


def test_allow_until_cap_then_block() -> None:
    limiter = SymbolDailyLimiter()
    now = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
    assert limiter.allow("BTCUSDT", limit=2, now=now)
    limiter.record("BTCUSDT", now=now)
    assert limiter.allow("BTCUSDT", limit=2, now=now)
    limiter.record("BTCUSDT", now=now)
    assert not limiter.allow("BTCUSDT", limit=2, now=now)


def test_no_limit_always_allows() -> None:
    limiter = SymbolDailyLimiter()
    now = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
    for _ in range(5):
        assert limiter.allow("ETHUSDT", limit=None, now=now)
        limiter.record("ETHUSDT", now=now)


def test_resets_at_utc_midnight() -> None:
    limiter = SymbolDailyLimiter()
    before = datetime(2026, 11, 23, 23, 59, tzinfo=UTC)
    after = datetime(2026, 11, 24, 0, 0, tzinfo=UTC)
    limiter.record("BTCUSDT", now=before)
    limiter.record("BTCUSDT", now=before)
    assert not limiter.allow("BTCUSDT", limit=2, now=before)
    assert limiter.allow("BTCUSDT", limit=2, now=after)
    assert limiter.count("BTCUSDT", now=after) == 0


def test_seed_replaces_counts() -> None:
    limiter = SymbolDailyLimiter()
    now = datetime(2026, 11, 23, 8, 0, tzinfo=UTC)
    limiter.seed({("bybit", "BTCUSDT"): 3}, now=now)
    assert limiter.count("BTCUSDT", now=now) == 3
    assert not limiter.allow("BTCUSDT", limit=3, now=now)


def test_profile_scoped_counters_are_independent() -> None:
    limiter = SymbolDailyLimiter()
    now = datetime(2026, 11, 23, 8, 0, tzinfo=UTC)
    limiter.record("BTCUSDT", profile_id="p1", now=now)
    assert not limiter.allow("BTCUSDT", profile_id="p1", limit=1, now=now)
    assert limiter.allow("BTCUSDT", profile_id="p2", limit=1, now=now)
    limiter.seed({("bybit", "BTCUSDT", "p2"): 2}, now=now)
    assert limiter.count("BTCUSDT", profile_id="p2", now=now) == 2
    assert limiter.count("BTCUSDT", profile_id="p1", now=now) == 0


def test_dispatch_skips_when_daily_limit_reached() -> None:
    class _Persist:
        def __init__(self) -> None:
            self.items = []

        def enqueue_signal(self, signal) -> None:
            self.items.append(signal)

    class _Notify:
        def __init__(self) -> None:
            self.items = []

        def enqueue(self, signal) -> None:
            self.items.append(signal)

    class _Engine:
        def __init__(self) -> None:
            self.settings = ScreenerSettings(symbol_daily_limits={"BTCUSDT": 1})

    persist = _Persist()
    notify = _Notify()
    state = AppState.__new__(AppState)
    state.signal_engine = _Engine()  # type: ignore[assignment]
    state.persistence = persist  # type: ignore[assignment]
    state.notifications = notify  # type: ignore[assignment]
    state.admin_exchanges = {"bybit"}
    state.trading = None
    state.symbol_daily_limiter = SymbolDailyLimiter()

    first = make_signal(SignalType.PUMP, 60, 50.0, symbol="BTCUSDT")
    second = make_signal(SignalType.PUMP, 60, 55.0, symbol="BTCUSDT")
    state.dispatch_signal(first)
    state.dispatch_signal(second)
    assert len(persist.items) == 1
    assert len(notify.items) == 1
