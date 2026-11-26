"""All monitoring profiles are evaluated in parallel; each gets its own alert."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from screener.application.correlator import SignalCorrelator
from screener.application.price_history import PriceHistoryManager
from screener.application.profile_monitor import ProfileMonitor
from screener.application.symbol_daily_limiter import SymbolDailyLimiter
from screener.app_state import AppState
from screener.config import MONITORING_CONFIG_SCHEMA_VERSION, ScreenerSettings
from screener.domain.enums import PriceEventKind, SignalType
from screener.domain.models import PriceEvent
from screener.domain.profiles import MonitoringProfile, ProfileVersion
from tests.helpers import T0, make_signal, settings


def _profile(name: str, cfg: ScreenerSettings) -> MonitoringProfile:
    pid = uuid4()
    vid = uuid4()
    version = ProfileVersion(
        id=vid,
        profile_id=pid,
        version_no=1,
        config=cfg.canonical_config(),
        config_schema_version=MONITORING_CONFIG_SCHEMA_VERSION,
        config_hash=cfg.config_hash(),
    )
    return MonitoringProfile(
        id=pid, name=name, description=None, is_system=False, current=version
    )


def _event(symbol: str, price: float, ts_ms: int, *, turnover: float = 1e6) -> PriceEvent:
    return PriceEvent(
        symbol=symbol,
        kind=PriceEventKind.LIVE,
        last_price=price,
        exchange_ts_ms=ts_ms,
        received_mono=0.0,
        turnover_24h=turnover,
        volume_24h=0.0,
    )


async def test_two_profiles_emit_two_named_signals() -> None:
    history = PriceHistoryManager()
    signals = []
    correlator = SignalCorrelator(
        window_ms=0, rule_version=lambda: "x", on_signal=signals.append
    )
    monitor = ProfileMonitor(history, correlator, settings())
    loose = settings(
        enable_long=True,
        long_threshold_pct=10.0,
        long_interval_sec=60,
        enable_dump=False,
        cooldown_sec=0,
        correlation_window_ms=0,
        min_turnover_24h_usdt=0,
    )
    tight = settings(
        enable_long=True,
        long_threshold_pct=20.0,
        long_interval_sec=60,
        enable_dump=False,
        cooldown_sec=0,
        correlation_window_ms=0,
        min_turnover_24h_usdt=0,
    )
    p_loose = _profile("Loose", loose)
    p_tight = _profile("Tight", tight)
    monitor.rebuild([p_loose, p_tight])

    history.record("AAAUSDT", ts_ms=T0, price=100.0)
    for sec in range(1, 61):
        history.record("AAAUSDT", ts_ms=T0 + sec * 1000, price=100.0)
    # +25% from 100 → both 10% and 20% rules fire
    event = _event("AAAUSDT", 125.0, T0 + 60_000)
    history.record(event.symbol, ts_ms=event.exchange_ts_ms, price=event.last_price)
    assert monitor.evaluate(event) >= 2
    assert len(signals) == 2
    names = {s.monitoring_profile_name for s in signals}
    assert names == {"Loose", "Tight"}
    assert {s.monitoring_profile_id for s in signals} == {p_loose.id, p_tight.id}


async def test_identical_configs_still_emit_per_profile() -> None:
    history = PriceHistoryManager()
    signals = []
    correlator = SignalCorrelator(
        window_ms=0, rule_version=lambda: "x", on_signal=signals.append
    )
    monitor = ProfileMonitor(history, correlator, settings())
    cfg = settings(
        enable_long=True,
        long_threshold_pct=5.0,
        long_interval_sec=60,
        enable_dump=False,
        cooldown_sec=0,
        correlation_window_ms=0,
        min_turnover_24h_usdt=0,
    )
    a = _profile("A", cfg)
    b = _profile("B", cfg)
    monitor.rebuild([a, b])
    history.record("BBBUSDT", ts_ms=T0, price=10.0)
    for sec in range(1, 61):
        history.record("BBBUSDT", ts_ms=T0 + sec * 1000, price=10.0)
    event = _event("BBBUSDT", 11.0, T0 + 60_000)
    history.record(event.symbol, ts_ms=event.exchange_ts_ms, price=event.last_price)
    monitor.evaluate(event)
    assert len(signals) == 2
    assert {s.monitoring_profile_name for s in signals} == {"A", "B"}


def test_daily_cap_is_per_profile() -> None:
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

    class _Monitor:
        def __init__(self, mapping: dict[str, ScreenerSettings]) -> None:
            self._mapping = mapping

        def settings_for(self, profile_id: str) -> ScreenerSettings | None:
            return self._mapping.get(profile_id)

    pid_a = uuid4()
    pid_b = uuid4()
    capped = ScreenerSettings(symbol_daily_limits={"BTCUSDT": 1})
    open_cap = ScreenerSettings(symbol_daily_limits={"BTCUSDT": 1})
    persist = _Persist()
    notify = _Notify()
    state = AppState.__new__(AppState)
    state.signal_engine = None  # type: ignore[assignment]
    state.persistence = persist  # type: ignore[assignment]
    state.notifications = notify  # type: ignore[assignment]
    state.admin_exchanges = {"bybit"}
    state.trading = None
    state.symbol_daily_limiter = SymbolDailyLimiter()
    state.profile_monitors = [_Monitor({str(pid_a): capped, str(pid_b): open_cap})]  # type: ignore[list-item]

    now = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
    first = make_signal(
        SignalType.PUMP, 60, 50.0, symbol="BTCUSDT", monitoring_profile_id=pid_a, detected_at=now
    )
    second = make_signal(
        SignalType.PUMP, 60, 55.0, symbol="BTCUSDT", monitoring_profile_id=pid_a, detected_at=now
    )
    other = make_signal(
        SignalType.PUMP, 60, 60.0, symbol="BTCUSDT", monitoring_profile_id=pid_b, detected_at=now
    )

    state.dispatch_monitor_signal(first)
    state.dispatch_monitor_signal(second)
    state.dispatch_monitor_signal(other)
    assert len(persist.items) == 2
    assert {s.monitoring_profile_id for s in persist.items} == {pid_a, pid_b}


def test_per_group_exclude_skips_symbol() -> None:
    history = PriceHistoryManager()
    signals = []
    correlator = SignalCorrelator(
        window_ms=0, rule_version=lambda: "x", on_signal=signals.append
    )
    monitor = ProfileMonitor(history, correlator, settings())
    cfg = settings(
        enable_long=True,
        long_threshold_pct=5.0,
        long_interval_sec=60,
        enable_dump=False,
        cooldown_sec=0,
        correlation_window_ms=0,
        min_turnover_24h_usdt=0,
        exclude_symbols={"SKIPUSDT"},
    )
    monitor.rebuild([_profile("Ex", cfg)])
    history.record("SKIPUSDT", ts_ms=T0, price=1.0)
    for sec in range(1, 61):
        history.record("SKIPUSDT", ts_ms=T0 + sec * 1000, price=1.0)
    event = _event("SKIPUSDT", 2.0, T0 + 60_000)
    history.record(event.symbol, ts_ms=event.exchange_ts_ms, price=event.last_price)
    assert monitor.evaluate(event) == 0
    assert signals == []
