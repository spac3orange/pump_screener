"""Load-ish check: ring history for hundreds of symbols stays bounded and O(1)."""

import time

from screener.application.price_history import PriceHistoryManager

T0 = 1_700_000_000_000


def test_history_memory_bound() -> None:
    h = PriceHistoryManager(capacity_sec=3700)
    symbols = [f"S{i}USDT" for i in range(800)]
    for sym in symbols:
        for t in range(100):
            h.record(sym, ts_ms=T0 + t * 1000, price=1.0 + t * 0.001)
    assert sum(h.point_count(s) for s in symbols) == 800 * 100
    for t in range(5000):
        h.record("BTCUSDT", ts_ms=T0 + t * 1000, price=1.0)
    assert h.point_count("BTCUSDT") <= 3700


def test_lookup_cost_independent_of_window_length() -> None:
    h = PriceHistoryManager()
    for t in range(3700):
        h.record("BTCUSDT", ts_ms=T0 + t * 1000, price=1.0)
    now = T0 + 3699 * 1000
    n = 20_000
    timings = {}
    for window in (60, 1800, 3600):
        start = time.perf_counter()
        for _ in range(n):
            h.lookup("BTCUSDT", now_ms=now, interval_sec=window)
        timings[window] = (time.perf_counter() - start) / n
    # Legacy deque scan grew linearly (1800s window ~30x a 60s window).
    assert timings[3600] < timings[60] * 3
