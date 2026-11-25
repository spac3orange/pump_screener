"""P0: commercial signals and subscriber code can never place or enqueue trades."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

from screener.app_state import AppState
from screener.domain.commercial import GroupSignal
from screener.domain.models import MarketSignal
from tests.helpers import T0, Harness, settings

SRC = Path(__file__).resolve().parents[2] / "src" / "screener"

COMMERCIAL_MODULES = [
    *sorted((SRC / "application" / "commercial").glob("*.py")),
    SRC / "commercial_runtime.py",
    SRC / "domain" / "commercial.py",
    SRC / "infrastructure" / "db" / "commercial_repo.py",
    SRC / "infrastructure" / "telegram" / "user_handlers.py",
    SRC / "infrastructure" / "telegram" / "payment_handlers.py",
    SRC / "infrastructure" / "telegram" / "user_keyboards.py",
    SRC / "infrastructure" / "telegram" / "commercial_texts.py",
]
FORBIDDEN_IMPORTS = (
    "bybit_client",
    "pybit",
    "screener.application.trading",
    "screener.infrastructure.bybit.lifecycle",
    "screener.infrastructure.db.trading_repo",
)
ORDER_CALLS = {"place_order", "create_order", "enqueue", "set_enabled"}


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _attr_calls(path: Path) -> set[str]:
    calls: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            calls.add(node.func.attr)
    return calls


def test_commercial_modules_do_not_import_trading() -> None:
    assert len(COMMERCIAL_MODULES) >= 15
    for path in COMMERCIAL_MODULES:
        bad = {m for m in _imports(path) if m.startswith(FORBIDDEN_IMPORTS)}
        assert not bad, f"{path.name} imports {bad}"


def test_commercial_modules_never_call_order_or_trading_entry_points() -> None:
    for path in COMMERCIAL_MODULES:
        found = _attr_calls(path) & ORDER_CALLS
        assert not found, f"{path.name} calls {found}"


def test_no_telegram_handler_places_orders() -> None:
    for path in (SRC / "infrastructure" / "telegram").glob("*.py"):
        found = _attr_calls(path) & {"place_order", "create_order"}
        assert not found, f"{path.name} calls {found}"


async def test_commercial_signals_never_reach_trading_or_admin_dispatch() -> None:
    # Subscriber group has far lower thresholds than the admin profile.
    h = Harness(commercial={"g": settings(long_threshold_pct=5.0, long_interval_sec=60)})
    enqueued: list[object] = []
    persisted: list[object] = []
    notified: list[object] = []
    state = AppState(
        system=None,  # type: ignore[arg-type]
        repo=None,  # type: ignore[arg-type]
        rest=None,  # type: ignore[arg-type]
        history=None,  # type: ignore[arg-type]
        processor=None,  # type: ignore[arg-type]
        signal_engine=h.engine,
        correlator=None,  # type: ignore[arg-type]
        registry=None,  # type: ignore[arg-type]
        ws_manager=None,  # type: ignore[arg-type]
        persistence=SimpleNamespace(enqueue_signal=persisted.append),  # type: ignore[arg-type]
        notifications=SimpleNamespace(enqueue=notified.append),  # type: ignore[arg-type]
        trading=SimpleNamespace(enqueue=enqueued.append),  # type: ignore[arg-type]
    )
    h.correlator.on_signal = state.dispatch_signal

    await h.send("AUSDT", "snapshot", T0, lastPrice="1.0", turnover24h="5000000")
    for t in range(1, 61):
        await h.send("AUSDT", "delta", T0 + t * 1000, lastPrice=f"{1.0 + t * 0.001:.4f}")
    await h.drain()

    assert h.group_signals, "subscriber rule must fire"
    assert all(isinstance(s, GroupSignal) for s in h.group_signals)
    assert enqueued == [] and persisted == [] and notified == []

    # The admin path still trades its own signals only.
    for t in range(61, 800):
        await h.send("AUSDT", "delta", T0 + t * 1000, lastPrice=f"{1.6 + t * 0.0001:.4f}")
    await h.drain()
    assert enqueued, "admin signal must still reach trading"
    assert all(isinstance(s, MarketSignal) for s in enqueued)
    assert not any(isinstance(s, GroupSignal) for s in enqueued)
