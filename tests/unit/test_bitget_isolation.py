"""Bitget feed never imports trading and never reaches TradingService.enqueue."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

from screener.app_state import AppState
from screener.application.bitget_processor import BitgetTickerProcessor
from screener.application.correlator import SignalCorrelator
from screener.application.metrics import FeedMetrics
from screener.application.price_history import PriceHistoryManager
from screener.application.signal_engine import SignalEngine
from screener.domain.enums import PriceEventKind, SignalType
from screener.domain.models import MarketSignal
from tests.helpers import T0, make_signal, settings

SRC = Path(__file__).resolve().parents[2] / "src" / "screener"
BITGET_MODULES = sorted((SRC / "infrastructure" / "bitget").glob("*.py"))
BITGET_MODULES.append(SRC / "application" / "bitget_processor.py")
FORBIDDEN_IMPORTS = (
    "bybit_client",
    "pybit",
    "screener.application.trading",
    "screener.infrastructure.bybit",
    "screener.infrastructure.db.trading_repo",
)


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_bitget_modules_do_not_import_trading() -> None:
    assert BITGET_MODULES
    for path in BITGET_MODULES:
        bad = {m for m in _imports(path) if m.startswith(FORBIDDEN_IMPORTS)}
        assert not bad, f"{path.name} imports {bad}"


def test_dispatch_monitor_signal_never_calls_trading() -> None:
    enqueued: list[object] = []
    persisted: list[object] = []
    notified: list[object] = []
    state = AppState(
        system=None,  # type: ignore[arg-type]
        repo=None,  # type: ignore[arg-type]
        rest=None,  # type: ignore[arg-type]
        history=None,  # type: ignore[arg-type]
        processor=None,  # type: ignore[arg-type]
        signal_engine=None,  # type: ignore[arg-type]
        correlator=None,  # type: ignore[arg-type]
        registry=None,  # type: ignore[arg-type]
        ws_manager=None,  # type: ignore[arg-type]
        persistence=SimpleNamespace(enqueue_signal=persisted.append),  # type: ignore[arg-type]
        notifications=SimpleNamespace(enqueue=notified.append),  # type: ignore[arg-type]
        trading=SimpleNamespace(enqueue=enqueued.append),  # type: ignore[arg-type]
    )
    sig = make_signal(SignalType.PUMP, 720, 41.0, exchange="bitget")
    state.dispatch_monitor_signal(sig)
    assert persisted == [sig] and notified == [sig] and enqueued == []

    state.admin_exchanges = {"bybit"}
    notified.clear()
    persisted.clear()
    state.dispatch_monitor_signal(sig)
    assert persisted == [sig] and notified == [] and enqueued == []


async def test_bitget_live_tick_reaches_notifications_not_trading() -> None:
    enqueued: list[object] = []
    persisted: list[object] = []
    notified: list[object] = []
    history = PriceHistoryManager()
    stats = FeedMetrics()
    proc = BitgetTickerProcessor(stats)
    proc.set_universe(["AUSDT"])
    cfg = settings(long_threshold_pct=5.0, long_interval_sec=60, correlation_window_ms=0)
    engine = SignalEngine(history, cfg)
    correlator = SignalCorrelator(
        window_ms=0,
        rule_version=lambda: engine.rule_version,
        exchange="bitget",
    )
    engine.on_matches = correlator.add
    state = AppState(
        system=None,  # type: ignore[arg-type]
        repo=None,  # type: ignore[arg-type]
        rest=None,  # type: ignore[arg-type]
        history=history,
        processor=None,  # type: ignore[arg-type]
        signal_engine=engine,
        correlator=correlator,
        registry=None,  # type: ignore[arg-type]
        ws_manager=None,  # type: ignore[arg-type]
        persistence=SimpleNamespace(enqueue_signal=persisted.append),  # type: ignore[arg-type]
        notifications=SimpleNamespace(enqueue=notified.append),  # type: ignore[arg-type]
        trading=SimpleNamespace(enqueue=enqueued.append),  # type: ignore[arg-type]
    )
    correlator.on_signal = state.dispatch_monitor_signal

    def _tick(ts_ms: int, price: str) -> None:
        events = proc.apply_message(
            {
                "arg": {"symbol": "AUSDT"},
                "data": [{"lastPrice": price, "turnover24h": "5000000", "volume24h": "1"}],
                "ts": ts_ms,
            }
        )
        for event in events or []:
            history.record(event.symbol, ts_ms=event.exchange_ts_ms, price=event.last_price)
            if event.kind is PriceEventKind.LIVE:
                engine.evaluate(event)

    _tick(T0, "1.0")
    for t in range(1, 70):
        _tick(T0 + t * 1000, f"{1.0 + t * 0.002:.4f}")
    correlator.flush_all()
    assert persisted, "Bitget admin rule must fire"
    assert all(isinstance(s, MarketSignal) and s.exchange == "bitget" for s in persisted)
    assert notified == persisted
    assert enqueued == []
