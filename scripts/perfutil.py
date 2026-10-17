"""Shared dev-only helpers for benchmark/soak scripts (not imported by the app)."""

from __future__ import annotations

import gc
import random
import sys
import time
from collections import Counter
from dataclasses import dataclass, field

import orjson

from screener.application.correlator import SignalCorrelator
from screener.application.market_processor import MarketDataProcessor
from screener.application.metrics import metrics
from screener.application.price_history import PriceHistoryManager
from screener.application.signal_engine import SignalEngine
from screener.config import ScreenerSettings

T0_SEC = 1_700_000_000
# Live Bybit ratio measured on 521 symbols: ~764 ticker frames/s, ~58 lastPrice changes/s.
FRAMES_PER_SYMBOL_SEC = 1.5
PRICE_CHANGE_SHARE = 0.077


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
        k32.K32GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(PMC),
            wintypes.DWORD,
        ]
        k32.K32GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
        return pmc.WorkingSetSize / 1024 / 1024
    with open("/proc/self/status") as fh:
        for line in fh:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    return 0.0


def run_isolated(script: str, counts: list[int], extra_args: list[str]) -> int:
    """Re-run ``script`` once per symbol count so each RSS reading starts clean."""
    import subprocess

    worst = 0
    for count in counts:
        proc = subprocess.run([sys.executable, script, "--symbols", str(count), *extra_args])
        worst = max(worst, proc.returncode)
    return worst


def top_types(limit: int = 8) -> list[tuple[str, int]]:
    gc.collect()
    return Counter(type(o).__name__ for o in gc.get_objects()).most_common(limit)


def percentiles_us(samples: list[float]) -> dict[str, float]:
    if not samples:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0}
    values = sorted(samples)
    n = len(values)
    return {
        "p50": round(values[n // 2] * 1e6, 2),
        "p95": round(values[min(n - 1, int(n * 0.95))] * 1e6, 2),
        "p99": round(values[min(n - 1, int(n * 0.99))] * 1e6, 2),
    }


@dataclass
class Workload:
    """Synthetic Bybit ticker stream through the production pipeline (no WS/DB)."""

    symbols: int
    seed: int = 1
    # Every N market seconds one symbol jumps +60% and reverts N/2 seconds later.
    spike_every_sec: int = 0
    settings: ScreenerSettings = field(
        default_factory=lambda: ScreenerSettings(min_turnover_24h_usdt=0)
    )

    def __post_init__(self) -> None:
        self.names = [f"S{i}USDT" for i in range(self.symbols)]
        self.history = PriceHistoryManager()
        self.processor = MarketDataProcessor(self.history)
        self.engine = SignalEngine(self.history, self.settings)
        self.signals = 0
        self.correlator = SignalCorrelator(
            window_ms=0,
            rule_version=lambda: self.engine.rule_version,
            on_signal=self._on_signal,
        )
        self.engine.on_matches = self.correlator.add
        self.rnd = random.Random(self.seed)
        self.prices = [1.0 + self.rnd.random() for _ in range(self.symbols)]
        self.sec = T0_SEC
        self.cs = 0
        self.pipeline_sec = 0.0
        self.frames = 0
        self.live_samples: list[float] = []

    def _on_signal(self, _signal) -> None:
        self.signals += 1

    def _feed(self, symbol: str, msg_type: str, data: dict) -> float:
        self.cs += 1
        raw = orjson.dumps(
            {
                "topic": f"tickers.{symbol}",
                "type": msg_type,
                "data": data,
                "cs": self.cs,
                "ts": self.sec * 1000,
            }
        )
        start = time.perf_counter()
        event = self.processor.apply_message(orjson.loads(raw), mono=start)
        if event is not None:
            self.history.record(event.symbol, ts_ms=event.exchange_ts_ms, price=event.last_price)
            self.engine.evaluate(event)
        elapsed = time.perf_counter() - start
        self.pipeline_sec += elapsed
        self.frames += 1
        return elapsed if event is not None else -1.0

    def snapshot_all(self) -> None:
        for i, name in enumerate(self.names):
            self._feed(
                name,
                "snapshot",
                {
                    "lastPrice": f"{self.prices[i]:.6f}",
                    "turnover24h": "50000000",
                    "volume24h": "1000000",
                    "markPrice": f"{self.prices[i]:.6f}",
                    "bid1Price": f"{self.prices[i]:.6f}",
                    "ask1Price": f"{self.prices[i]:.6f}",
                },
            )

    def prefill_history(self) -> None:
        cap = self.history.capacity_sec
        for i, name in enumerate(self.names):
            self.history.backfill_klines(
                name, [(self.sec - cap + 5, self.prices[i])], hi_sec=self.sec - 1
            )

    def step_second(self, *, keep_samples: bool = True) -> None:
        """One market second: enrichment deltas plus a share of lastPrice changes."""
        self.sec += 1
        rnd = self.rnd
        if self.spike_every_sec:
            self._spike()
        frames = int(self.symbols * FRAMES_PER_SYMBOL_SEC)
        for _ in range(frames):
            i = rnd.randrange(self.symbols)
            if rnd.random() < PRICE_CHANGE_SHARE:
                self.prices[i] *= 1.0 + (rnd.random() - 0.5) * 0.002
                data = {"lastPrice": f"{self.prices[i]:.6f}", "turnover24h": "50000000"}
            else:
                data = {"bid1Price": f"{self.prices[i]:.6f}", "openInterest": "123.4"}
            sample = self._feed(self.names[i], "delta", data)
            if keep_samples and sample >= 0:
                self.live_samples.append(sample)

    def _spike(self) -> None:
        every = self.spike_every_sec
        phase = self.sec % every
        if phase not in (0, every // 2):
            return
        i = (self.sec // every) % self.symbols
        self.prices[i] *= 1.6 if phase == 0 else 1 / 1.6
        self._feed(self.names[i], "delta", {"lastPrice": f"{self.prices[i]:.6f}"})

    def counters(self) -> dict[str, int]:
        return {
            "live": metrics.live_price_events_total,
            "evals": metrics.engine_evaluations_total,
            "lookups": metrics.history_lookups_total,
        }


@dataclass
class DualWorkload:
    """Bybit ticker stream plus synthetic Binance ``!ticker@arr`` frames."""

    bybit_symbols: int = 500
    binance_symbols: int = 523
    seed: int = 1
    settings: ScreenerSettings = field(
        default_factory=lambda: ScreenerSettings(min_turnover_24h_usdt=0)
    )

    def __post_init__(self) -> None:
        from screener.application.binance_processor import BinanceTickerProcessor

        self.bybit = Workload(self.bybit_symbols, seed=self.seed, settings=self.settings)
        self.binance_names = [f"B{i}USDT" for i in range(self.binance_symbols)]
        self.binance_history = PriceHistoryManager()
        self.binance_proc = BinanceTickerProcessor(
            metrics.feed("binance"), self.binance_history
        )
        self.binance_proc.set_universe(self.binance_names)
        self.binance_engine = SignalEngine(self.binance_history, self.settings)
        self.binance_prices = [1.0 + self.bybit.rnd.random() for _ in range(self.binance_symbols)]
        self.binance_seeded = False
        self.binance_frames = 0
        self.binance_pipeline_sec = 0.0

    def snapshot_all(self) -> None:
        self.bybit.snapshot_all()
        self._binance_frame(seed=True)

    def prefill_history(self) -> None:
        self.bybit.prefill_history()
        cap = self.binance_history.capacity_sec
        sec = self.bybit.sec
        for i, name in enumerate(self.binance_names):
            self.binance_history.backfill_klines(
                name, [(sec - cap + 5, self.binance_prices[i])], hi_sec=sec - 1
            )

    def step_second(self, *, keep_samples: bool = True) -> None:
        self.bybit.step_second(keep_samples=keep_samples)
        self._binance_frame(seed=False)

    def _binance_frame(self, *, seed: bool) -> None:
        rnd = self.bybit.rnd
        ts_ms = self.bybit.sec * 1000
        n = self.binance_symbols if seed else min(220, self.binance_symbols)
        items = []
        for _ in range(n):
            i = rnd.randrange(self.binance_symbols)
            if seed or rnd.random() < 0.15:
                self.binance_prices[i] *= 1.0 + (rnd.random() - 0.5) * 0.002
            items.append(
                {
                    "s": self.binance_names[i],
                    "E": ts_ms + i,
                    "c": f"{self.binance_prices[i]:.6f}",
                    "q": "50000000",
                    "v": "1000000",
                }
            )
        start = time.perf_counter()
        events = self.binance_proc.apply_message({"data": items}, mono=start)
        for event in events or []:
            self.binance_history.record(
                event.symbol, ts_ms=event.exchange_ts_ms, price=event.last_price
            )
            if event.kind.name == "LIVE":
                self.binance_engine.evaluate(event)
        self.binance_pipeline_sec += time.perf_counter() - start
        self.binance_frames += 1
