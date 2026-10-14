"""Repeatable synthetic performance benchmark (no WS/DB).

Runs the production processor -> history -> SignalEngine -> correlator path on a
deterministic Bybit-like ticker stream with history pre-filled to full depth.

Usage: python scripts/benchmark_performance.py [--symbols 500 900] [--seconds 120]
"""

from __future__ import annotations

import argparse
import time

from perfutil import Workload, percentiles_us, rss_mb, run_isolated, top_types

from screener.application.metrics import metrics


def run(symbols: int, seconds: int, *, show_types: bool) -> None:
    metrics.__init__()
    rss0 = rss_mb()
    wl = Workload(symbols)
    wl.snapshot_all()
    wl.prefill_history()
    rss_history = rss_mb()

    c0 = wl.counters()
    pipe0, frames0 = wl.pipeline_sec, wl.frames
    cpu0, wall0 = time.process_time(), time.perf_counter()
    for _ in range(seconds):
        wl.step_second()
    wall = time.perf_counter() - wall0
    cpu = time.process_time() - cpu0
    c1 = wl.counters()
    pipe = wl.pipeline_sec - pipe0
    frames = wl.frames - frames0

    lookups = c1["lookups"] - c0["lookups"]
    evals = c1["evals"] - c0["evals"]
    lookup_us = _lookup_us(wl)
    print(
        f"symbols={symbols} market_seconds={seconds} frames={frames} "
        f"live={c1['live'] - c0['live']} evals={evals} lookups={lookups} signals={wl.signals}"
    )
    print(
        f"  rss_start_mb={rss0:.1f} rss_after_history_mb={rss_history:.1f} "
        f"history_mb={rss_history - rss0:.1f} rss_end_mb={rss_mb():.1f}"
    )
    print(
        f"  pipeline_frames_per_sec={frames / pipe:,.0f} "
        f"engine_evals_per_sec={evals / pipe:,.0f} "
        f"pipeline_us_per_frame={pipe / frames * 1e6:.2f} "
        f"wall_s={wall:.2f} cpu_s={cpu:.2f}"
    )
    print(f"  lookup_us={lookup_us:.2f} live_event_us={percentiles_us(wl.live_samples)}")
    if show_types:
        print(f"  top_types={top_types()}")


def _lookup_us(wl: Workload, n: int = 50_000) -> float:
    names = wl.names
    now_ms = wl.sec * 1000
    start = time.perf_counter()
    for i in range(n):
        wl.history.lookup(names[i % len(names)], now_ms=now_ms, interval_sec=1800)
    return (time.perf_counter() - start) / n * 1e6


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", type=int, nargs="+", default=[500, 900])
    parser.add_argument("--seconds", type=int, default=120)
    parser.add_argument("--types", action="store_true", help="print top object types")
    args = parser.parse_args()
    if len(args.symbols) > 1:
        extra = ["--seconds", str(args.seconds)] + (["--types"] if args.types else [])
        raise SystemExit(run_isolated(__file__, args.symbols, extra))
    run(args.symbols[0], args.seconds, show_types=args.types)


if __name__ == "__main__":
    main()
