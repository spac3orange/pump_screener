"""MultiConfigEngine must give every group exactly what SignalEngine gives that config."""

from __future__ import annotations

import random

import pytest

from screener.application.commercial.multi_engine import MultiConfigEngine
from screener.application.price_history import PriceHistoryManager
from screener.domain.enums import PriceEventKind, SignalType
from screener.domain.models import PriceEvent
from tests.helpers import T0, Harness, settings
from tests.unit.test_signal_golden import SECONDS, SYMBOLS, _scripted


def _norm(signals) -> list[tuple]:
    return [
        (
            s.symbol,
            s.signal_type.value,
            s.interval_sec,
            round(s.change_pct, 9),
            round(s.price, 9),
            s.level,
            tuple(sorted(f"{m.signal_type.value}:{m.interval_sec}:{m.level}" for m in s.matches)),
            s.detected_at,
        )
        for s in signals
    ]


async def _golden_path(h: Harness) -> None:
    rnd = random.Random(42)
    base = [100.0 * (1 + i / 10) for i in range(len(SYMBOLS))]
    prices = list(base)
    for i, sym in enumerate(SYMBOLS):
        await h.send(sym, "snapshot", T0, lastPrice=f"{prices[i]:.6f}", turnover24h="5000000")
    for t in range(1, SECONDS + 1):
        ts = T0 + t * 1000
        for i, sym in enumerate(SYMBOLS):
            new = _scripted(i, t, prices[i], base[i])
            if new == prices[i] and rnd.random() < 0.2 and i not in (5, 9):
                new = prices[i] * (1 + (rnd.random() - 0.5) * 0.006)
            if new != prices[i]:
                prices[i] = new
                await h.send(sym, "delta", ts, lastPrice=f"{new:.6f}")
        if t == 1200:
            await h.send("G11USDT", "delta", ts + 100, lastPrice=f"{base[11] * 1.5:.6f}")
            await h.send("G11USDT", "delta", ts + 200, lastPrice=f"{base[11] * 1.2:.6f}")
            prices[11] = float(f"{base[11] * 1.2:.6f}")
        await h.drain()


async def test_single_group_matches_admin_engine_on_golden_path() -> None:
    h = Harness(commercial={"g": settings()})
    await _golden_path(h)
    assert h.signals, "golden path must produce signals"
    assert _norm(h.group_signals) == _norm(h.signals)
    assert {s.group_key for s in h.group_signals} == {"g"}


async def _random_stream(h: Harness, seed: int, symbols: int, seconds: int) -> None:
    rnd = random.Random(seed)
    names = [f"R{i:02d}USDT" for i in range(symbols)]
    prices = [10.0 + i for i in range(symbols)]
    for i, sym in enumerate(names):
        turnover = "50000" if i % 4 == 0 else "5000000"
        await h.send(sym, "snapshot", T0, lastPrice=f"{prices[i]:.6f}", turnover24h=turnover)
    for t in range(1, seconds + 1):
        ts = T0 + t * 1000
        for i, sym in enumerate(names):
            r = rnd.random()
            if r < 0.02:
                prices[i] *= 1 + rnd.choice((-1, 1)) * rnd.uniform(0.03, 0.15)
            elif r < 0.5:
                prices[i] *= 1 + (rnd.random() - 0.5) * 0.01
            else:
                continue
            await h.send(sym, "delta", ts, lastPrice=f"{prices[i]:.6f}")
        await h.drain()


def _random_configs(seed: int, n: int) -> dict[str, object]:
    rnd = random.Random(seed)
    configs = {}
    for i in range(n):
        cfg = settings(
            enable_long=rnd.random() < 0.8,
            long_threshold_pct=rnd.choice((2.0, 3.0, 5.0, 8.0)),
            long_interval_sec=rnd.choice((60, 120, 300)),
            enable_short=rnd.random() < 0.8,
            short_threshold_pct=rnd.choice((2.0, 4.0, 6.0)),
            short_interval_sec=rnd.choice((60, 180, 300)),
            enable_dump=rnd.random() < 0.8,
            dump_threshold_pct=rnd.choice((5.0, 10.0)),
            dump_interval_sec=rnd.choice((60, 300)),
            min_turnover_24h_usdt=rnd.choice((0.0, 100_000.0)),
            cooldown_sec=rnd.choice((0, 30, 120)),
            correlation_window_ms=0,
        )
        configs[f"k{i}"] = cfg
    return configs


@pytest.mark.parametrize("seed", [1, 2, 3])
async def test_n_groups_equal_n_independent_engines(seed: int) -> None:
    configs = _random_configs(seed, 6)
    shared = Harness(commercial=configs)
    await _random_stream(shared, seed, symbols=8, seconds=420)
    by_group: dict[str, list] = {k: [] for k in configs}
    for s in shared.group_signals:
        by_group[s.group_key].append(s)
    total = 0
    for key, cfg in configs.items():
        alone = Harness(**cfg.model_dump())
        await _random_stream(alone, seed, symbols=8, seconds=420)
        assert _norm(by_group[key]) == _norm(alone.signals), key
        total += len(alone.signals)
    assert total > 0


def _event(symbol: str, sec: int, price: float, turnover: float = 1e9) -> PriceEvent:
    return PriceEvent(
        symbol=symbol,
        kind=PriceEventKind.LIVE,
        last_price=price,
        exchange_ts_ms=T0 + sec * 1000,
        received_mono=0.0,
        turnover_24h=turnover,
    )


def test_set_groups_keeps_state_of_surviving_groups_and_drops_removed() -> None:
    history = PriceHistoryManager()
    cfg_a = settings(
        long_threshold_pct=5.0, long_interval_sec=60, enable_short=False, enable_dump=False
    )
    cfg_b = settings(
        long_threshold_pct=6.0, long_interval_sec=60, enable_short=False, enable_dump=False
    )
    hits: list[tuple[str, SignalType]] = []
    engine = MultiConfigEngine(
        history, cfg_a, on_matches=lambda e, k, ms: hits.extend((k, m.signal_type) for m in ms)
    )
    engine.set_groups({"a": cfg_a, "b": cfg_b})
    for sec in range(0, 61):
        history.record("XUSDT", ts_ms=T0 + sec * 1000, price=1.0)
    history.record("XUSDT", ts_ms=T0 + 61_000, price=1.10)
    engine.evaluate(_event("XUSDT", 61, 1.10))
    assert sorted(hits) == [("a", SignalType.PUMP), ("b", SignalType.PUMP)]
    assert engine.active_states == 2

    engine.set_groups({"a": cfg_a})
    assert engine.active_states == 1 and engine.group_count == 1
    hits.clear()
    history.record("XUSDT", ts_ms=T0 + 62_000, price=1.11)
    engine.evaluate(_event("XUSDT", 62, 1.11))
    assert hits == []  # "a" kept its cooldown state across the swap

    engine.clear_symbol("XUSDT")
    assert engine.active_states == 0


def test_no_groups_is_a_noop_and_excluded_symbols_skip() -> None:
    history = PriceHistoryManager()
    engine = MultiConfigEngine(history, settings(exclude_symbols={"XUSDT"}))
    assert engine.evaluate(_event("AUSDT", 1, 1.0)) == 0
    engine.set_groups({"a": settings(exclude_symbols={"XUSDT"})})
    assert engine.evaluate(_event("XUSDT", 1, 1.0)) == 0
    assert engine.max_interval_sec == 900
