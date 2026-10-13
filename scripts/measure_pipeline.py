"""Live Bybit pipeline measurement without the database.

Runs the production WS manager, processor, ordered price queue, history,
engine and correlator on the real ticker stream with history pre-filled to the
full window, then reports CPU, RSS and pipeline metrics.

Usage: python scripts/measure_pipeline.py [--duration 60] [--profile]
"""

from __future__ import annotations

import argparse
import asyncio
import cProfile
import io
import pstats
import sys
import time
from types import SimpleNamespace

from screener.application.correlator import SignalCorrelator
from screener.application.market_processor import MarketDataProcessor
from screener.application.metrics import metrics
from screener.application.price_history import PriceHistoryManager
from screener.application.signal_engine import SignalEngine
from screener.config import ScreenerSettings
from screener.infrastructure.bybit.mapping import is_crypto_usdt_linear_perpetual
from screener.infrastructure.bybit.rest import BybitRestClient
from screener.infrastructure.bybit.ws import WebSocketManager
from screener.workers.market_worker import MarketWorker


def rss_mb() -> float:
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
        k32.K32GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
        return pmc.WorkingSetSize / 1024 / 1024
    with open("/proc/self/status") as fh:
        for line in fh:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    return 0.0


async def main(duration: float, profile: bool) -> None:
    rest = BybitRestClient("https://api.bybit.com")
    raw = await rest.fetch_linear_instruments()
    symbols = sorted(r["symbol"] for r in raw if is_crypto_usdt_linear_perpetual(r))
    rss_base = rss_mb()

    history = PriceHistoryManager()
    processor = MarketDataProcessor(history)
    engine = SignalEngine(history, ScreenerSettings())
    signals: list = []
    correlator = SignalCorrelator(
        window_ms=400, rule_version=lambda: engine.rule_version, on_signal=signals.append
    )
    engine.on_matches = correlator.add
    state = SimpleNamespace(
        system=SimpleNamespace(market_queue_maxsize=10_000),
        processor=processor,
        history=history,
        signal_engine=engine,
        correlator=correlator,
    )
    worker = MarketWorker(state)  # type: ignore[arg-type]
    ws = WebSocketManager(
        "wss://stream.bybit.com/v5/public/linear",
        on_message=worker.on_ws_message,
        on_connect=processor.on_resubscribe,
        on_disconnect=lambda syms, ts: history.mark_disconnected(syms, at_ts_ms=ts),
    )
    process_task = asyncio.create_task(worker._process_loop())
    monitor_task = asyncio.create_task(worker._loop_monitor())
    await ws.start(symbols)
    await asyncio.sleep(8)

    # Pre-fill a full hour of history at the current price (steady-state lookup depth).
    for symbol in history.symbols():
        span = history.span(symbol)
        price = history.latest_price(symbol)
        if span and price:
            first = span[0]
            history.backfill_klines(
                symbol, [(first - history.capacity_sec + 5, price)], hi_sec=first - 1
            )

    start_counts = {
        "frames": metrics.ws_frames_total,
        "ticker": metrics.ticker_messages_total,
        "live": metrics.live_price_events_total,
        "evals": metrics.engine_evaluations_total,
        "lookups": metrics.history_lookups_total,
    }
    metrics._latency_n = 0
    await asyncio.sleep(1)
    metrics.event_loop_lag_max_ms = 0.0
    profiler = cProfile.Profile() if profile else None
    cpu0, t0 = time.process_time(), time.time()
    if profiler:
        profiler.enable()
    await asyncio.sleep(duration)
    if profiler:
        profiler.disable()
    elapsed = time.time() - t0
    cpu = time.process_time() - cpu0

    def rate(key: str, attr: str) -> float:
        return round((getattr(metrics, attr) - start_counts[key]) / elapsed, 1)

    print(f"symbols={len(symbols)} seconds={elapsed:.1f} profiler={'on' if profile else 'off'}")
    print(f"cpu_pct={cpu / elapsed * 100:.1f}")
    print(f"rss_mb_total={rss_mb():.1f} rss_mb_before_history={rss_base:.1f}")
    print(f"ws_frames_per_sec={rate('frames', 'ws_frames_total')}")
    print(f"ticker_msgs_per_sec={rate('ticker', 'ticker_messages_total')}")
    print(f"live_last_price_events_per_sec={rate('live', 'live_price_events_total')}")
    print(f"engine_evals_per_sec={rate('evals', 'engine_evaluations_total')}")
    print(f"history_lookups_per_sec={rate('lookups', 'history_lookups_total')}")
    print(f"pipeline_latency_ms={metrics.latency_percentiles_ms()}")
    print(f"event_loop_lag_max_ms={metrics.event_loop_lag_max_ms:.2f}")
    print(
        f"reconnects={metrics.ws_reconnects} gaps={metrics.history_gaps_total} "
        f"raw_protocol_drops={metrics.raw_protocol_drops_total} "
        f"lost_price_events={metrics.lost_price_events} "
        f"price_events_in={metrics.price_events_in} processed={metrics.price_events_processed} "
        f"deltas_before_snapshot={metrics.deltas_before_snapshot_total} "
        f"queue_blocked={metrics.price_queue_blocked_total}"
    )
    print(f"signals={len(signals)}")
    await ws.stop()
    process_task.cancel()
    monitor_task.cancel()
    await rest.aclose()
    if profiler:
        out = io.StringIO()
        pstats.Stats(profiler, stream=out).sort_stats("tottime").print_stats(15)
        print(out.getvalue()[:5000])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--profile", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.duration, args.profile))
