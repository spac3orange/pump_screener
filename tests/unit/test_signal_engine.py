import asyncio

import pytest

from screener.application.metrics import metrics
from screener.domain.enums import Direction, SignalType
from tests.helpers import T0, Harness


def _at(sec: float) -> int:
    return T0 + int(sec * 1000)


async def _seed(h: Harness, symbol: str, price: float) -> None:
    await h.send(symbol, "snapshot", _at(0), lastPrice=str(price), turnover24h="1000000")


@pytest.mark.asyncio
async def test_small_pump_does_not_fire() -> None:
    h = Harness()
    await _seed(h, "BTCUSDT", 100.0)
    await h.send("BTCUSDT", "delta", _at(720), lastPrice="105")
    await h.drain()
    assert h.signals == []


@pytest.mark.asyncio
async def test_long_fires_at_40_percent_in_12_minutes() -> None:
    h = Harness()
    await _seed(h, "BTCUSDT", 100.0)
    await h.send("BTCUSDT", "delta", _at(720), lastPrice="140")
    await h.drain()
    assert len(h.signals) == 1
    signal = h.signals[0]
    assert signal.signal_type is SignalType.PUMP
    assert signal.direction is Direction.UP
    assert signal.interval_sec == 720
    assert signal.change_pct == pytest.approx(40.0)


@pytest.mark.asyncio
async def test_overshoot_above_threshold_still_fires() -> None:
    """1min/10%: a 50% move must fire; only moves below 10% are ignored."""
    h = Harness(
        enable_dump=False,
        long_threshold_pct=10.0,
        long_interval_sec=60,
    )
    await _seed(h, "BTCUSDT", 100.0)
    await h.send("BTCUSDT", "delta", _at(60), lastPrice="150")
    await h.drain()
    assert len(h.signals) == 1
    assert h.signals[0].change_pct == pytest.approx(50.0)


@pytest.mark.asyncio
async def test_below_threshold_does_not_fire() -> None:
    h = Harness(
        enable_dump=False,
        long_threshold_pct=10.0,
        long_interval_sec=60,
    )
    await _seed(h, "BTCUSDT", 100.0)
    await h.send("BTCUSDT", "delta", _at(60), lastPrice="109.9")
    await h.drain()
    assert h.signals == []


@pytest.mark.asyncio
async def test_short_rule_never_fires_even_if_enabled_in_settings() -> None:
    h = Harness(enable_long=False, enable_dump=False, enable_short=True)
    await _seed(h, "ETHUSDT", 100.0)
    await h.send("ETHUSDT", "delta", _at(1800), lastPrice="90")
    await h.drain()
    assert h.signals == []


@pytest.mark.asyncio
async def test_dump_fires_at_minus_40_percent_in_15_minutes() -> None:
    h = Harness(enable_long=False)
    await _seed(h, "SOLUSDT", 100.0)
    await h.send("SOLUSDT", "delta", _at(900), lastPrice="60")
    await h.drain()
    assert [s.signal_type for s in h.signals] == [SignalType.DUMP]
    assert h.signals[0].interval_sec == 900


@pytest.mark.asyncio
async def test_disabled_rule_never_fires() -> None:
    h = Harness(enable_long=False)
    await _seed(h, "BTCUSDT", 100.0)
    await h.send("BTCUSDT", "delta", _at(720), lastPrice="150")
    await h.drain()
    assert h.signals == []


@pytest.mark.asyncio
async def test_no_spam_on_small_increments() -> None:
    h = Harness(enable_dump=False)
    await _seed(h, "SOLUSDT", 100.0)
    await h.send("SOLUSDT", "delta", _at(720), lastPrice="140")
    await h.send("SOLUSDT", "delta", _at(721), lastPrice="140.2")
    await h.send("SOLUSDT", "delta", _at(722), lastPrice="140.5")
    await h.drain()
    assert len(h.signals) == 1


@pytest.mark.asyncio
async def test_snapshot_seed_does_not_trigger_evaluation() -> None:
    h = Harness()
    await _seed(h, "XUSDT", 100.0)
    before = metrics.engine_evaluations_total
    # Snapshot after reconnect with a big move must not count as a live trade.
    await h.send("XUSDT", "snapshot", _at(720), lastPrice="200")
    await h.drain()
    assert metrics.engine_evaluations_total == before
    assert h.signals == []


@pytest.mark.asyncio
async def test_enrichment_delta_does_not_evaluate() -> None:
    h = Harness()
    await _seed(h, "YUSDT", 100.0)
    before = metrics.engine_evaluations_total
    await h.send("YUSDT", "delta", _at(10), bid1Price="99", openInterest="1")
    await h.drain()
    assert metrics.engine_evaluations_total == before
    assert h.worker.queue.qsize() == 0


@pytest.mark.asyncio
async def test_one_lookup_per_unique_window() -> None:
    h = Harness(long_interval_sec=900, dump_interval_sec=900)
    await _seed(h, "ZUSDT", 100.0)
    await h.drain()
    await h.send("ZUSDT", "delta", _at(900), lastPrice="101")
    live = h.worker.queue.get_nowait()
    h.history.record(live.symbol, ts_ms=live.exchange_ts_ms, price=live.last_price)
    before = metrics.history_lookups_total
    h.engine.evaluate(live)
    assert metrics.history_lookups_total - before == 1


@pytest.mark.asyncio
async def test_fast_spike_100_141_120_is_detected() -> None:
    """Regression: a threshold crossing that reverts before processing must not be lost."""
    h = Harness(enable_dump=False)
    await _seed(h, "SPKUSDT", 100.0)
    await h.drain()
    # Both live events are queued before the first is processed.
    await h.send("SPKUSDT", "delta", _at(720), lastPrice="141")
    await h.send("SPKUSDT", "delta", _at(720.05), lastPrice="120")
    assert h.worker.queue.qsize() == 2
    await h.drain()
    pumps = [s for s in h.signals if s.signal_type is SignalType.PUMP]
    assert len(pumps) == 1
    assert pumps[0].price == 141.0
    assert pumps[0].change_pct == pytest.approx(41.0)
    assert metrics.lost_price_events == 0
    assert metrics.raw_protocol_drops_total == 0


@pytest.mark.asyncio
async def test_dump_overshoot_correlates_to_one_signal() -> None:
    h = Harness(enable_long=False, correlation_window_ms=50, dump_threshold_pct=20.0)
    await _seed(h, "XYZUSDT", 100.0)
    await h.send("XYZUSDT", "delta", _at(900), lastPrice="75")  # -25%
    await h.send("XYZUSDT", "delta", _at(900.2), lastPrice="58")  # -42%
    await h.drain()
    assert h.signals == []  # still inside the correlation window
    await asyncio.sleep(0.1)
    assert len(h.signals) == 1
    signal = h.signals[0]
    assert signal.primary.signal_type is SignalType.DUMP
    assert {m.signal_type for m in signal.matches} == {SignalType.DUMP}


@pytest.mark.asyncio
async def test_moves_further_apart_than_window_stay_separate() -> None:
    h = Harness(
        enable_long=False,
        correlation_window_ms=30,
        dump_threshold_pct=10.0,
        cooldown_sec=0,
        escalate_step_pct=0.1,
    )
    await _seed(h, "SEPUSDT", 100.0)
    await h.send("SEPUSDT", "delta", _at(900), lastPrice="85")
    await h.drain()
    await asyncio.sleep(0.08)
    await h.send("SEPUSDT", "delta", _at(900.5), lastPrice="50")
    await h.drain()
    await asyncio.sleep(0.08)
    assert [s.signal_type for s in h.signals] == [SignalType.DUMP, SignalType.DUMP]
