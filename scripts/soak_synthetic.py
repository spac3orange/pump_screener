"""Accelerated synthetic soak: RSS must plateau after warmup.

Simulates N market seconds (default 3600 = 1h of market time) through the
production pipeline as fast as possible and samples RSS at checkpoints.

Usage: python scripts/soak_synthetic.py [--symbols 500 900] [--market-seconds 3600]
"""

from __future__ import annotations

import argparse
import time

from perfutil import Workload, rss_mb, run_isolated, top_types

from screener.application.metrics import metrics

CHECKPOINTS_MIN = (0, 10, 30, 60)


def run(symbols: int, market_seconds: int, *, tolerance_mb: float) -> bool:
    metrics.__init__()
    rss_start = rss_mb()
    wl = Workload(symbols, spike_every_sec=20)
    wl.snapshot_all()
    # Warmup: one full history capacity of live seconds, no pre-fill.
    warmup = wl.history.capacity_sec
    for _ in range(warmup):
        wl.step_second(keep_samples=False)
    rss_warm = rss_mb()
    samples = {"startup": rss_start, "warmup": rss_warm}
    started = time.perf_counter()
    for sec in range(1, market_seconds + 1):
        wl.step_second(keep_samples=False)
        minute = sec / 60
        if minute in CHECKPOINTS_MIN:
            samples[f"+{int(minute)}m"] = rss_mb()
    samples["end"] = rss_mb()
    growth = samples["end"] - rss_warm
    ok = growth <= tolerance_mb
    print(
        f"symbols={symbols} warmup_s={warmup} market_s={market_seconds} "
        f"wall_s={time.perf_counter() - started:.1f} frames={wl.frames} signals={wl.signals}"
    )
    print("  rss_mb=" + " ".join(f"{k}:{v:.1f}" for k, v in samples.items()))
    print(f"  growth_after_warmup_mb={growth:.2f} plateau={'OK' if ok else 'FAIL'}")
    print(f"  top_types={top_types(6)}")
    return ok


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", type=int, nargs="+", default=[500, 900])
    parser.add_argument("--market-seconds", type=int, default=3600)
    parser.add_argument("--tolerance-mb", type=float, default=3.0)
    args = parser.parse_args()
    if len(args.symbols) > 1:
        extra = [
            "--market-seconds",
            str(args.market_seconds),
            "--tolerance-mb",
            str(args.tolerance_mb),
        ]
        raise SystemExit(run_isolated(__file__, args.symbols, extra))
    ok = run(args.symbols[0], args.market_seconds, tolerance_mb=args.tolerance_mb)
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
