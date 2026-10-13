"""Synthetic throughput benchmark for PriceHistory + SignalEngine.

Usage: python scripts/bench_history.py [--symbols 500 900] [--events 200000]
"""

from __future__ import annotations

import argparse
import random
import time

from screener.application.price_history import PriceHistoryManager
from screener.application.signal_engine import SignalEngine
from screener.config import ScreenerSettings
from screener.domain.enums import PriceEventKind
from screener.domain.models import PriceEvent

T0 = 1_700_000_000_000


def run(symbols: int, events: int) -> None:
    history = PriceHistoryManager()
    names = [f"S{i}USDT" for i in range(symbols)]
    start = time.perf_counter()
    for name in names:
        history.backfill_klines(name, [(T0 // 1000, 1.0)], lo_sec=T0 // 1000, hi_sec=T0 // 1000 + 3700)
    fill = time.perf_counter() - start

    engine = SignalEngine(history, ScreenerSettings(min_turnover_24h_usdt=0))
    rnd = random.Random(1)
    now_ms = T0 + 3700_000
    batch = [
        PriceEvent(
            symbol=names[i % symbols],
            kind=PriceEventKind.LIVE,
            last_price=1.0 + rnd.random() * 0.01,
            exchange_ts_ms=now_ms,
            received_mono=0.0,
            turnover_24h=1e9,
        )
        for i in range(events)
    ]

    start = time.perf_counter()
    for name in names:
        history.lookup(name, now_ms=now_ms, interval_sec=1800)
    for _ in range(max(1, events // symbols - 1)):
        for name in names:
            history.lookup(name, now_ms=now_ms, interval_sec=1800)
    lookups = max(1, events // symbols) * symbols
    lookup_us = (time.perf_counter() - start) / lookups * 1e6

    start = time.perf_counter()
    for event in batch:
        history.record(event.symbol, ts_ms=event.exchange_ts_ms, price=event.last_price)
        engine.evaluate(event)
    elapsed = time.perf_counter() - start
    print(
        f"symbols={symbols} prefill={fill:.2f}s lookup_1800s={lookup_us:.2f}us "
        f"record+evaluate={elapsed / events * 1e6:.2f}us/event "
        f"throughput={events / elapsed:,.0f} events/s (3 rules, 3 windows)"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", type=int, nargs="+", default=[500, 900])
    parser.add_argument("--events", type=int, default=200_000)
    args = parser.parse_args()
    for symbols in args.symbols:
        run(symbols, args.events)


if __name__ == "__main__":
    main()
