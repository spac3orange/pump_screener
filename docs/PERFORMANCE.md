# Performance: measurements and how to reproduce

Rule: no optimization without a measurement; correctness first. Raw lastPrice events are never
coalesced or dropped (guarded by `tests/unit/test_signal_golden.py`).

## Scripts

| Script | What it measures |
|--------|------------------|
| `scripts/measure_pipeline.py --duration 25 [--profile] [--compression none] [--max-queue N]` | live Bybit public WS: CPU %, RSS, frames/s, evals/s, pipeline p50/p95/p99, event-loop lag, drops |
| `scripts/benchmark_performance.py --seconds 300 [--types]` | synthetic full pipeline (orjson → processor → history → engine → correlator), each symbol count in a fresh subprocess |
| `scripts/benchmark_performance.py --dual --seconds 120` | Bybit 500 + Binance 523 synthetic feeds together (~+34 MB PriceHistory for ~523 Binance symbols). Bitget adds its own history: about 30 MB more for ~466 USDT-M crypto perpetuals |
| `scripts/soak_synthetic.py` | 3840 s warmup + 3600 s market at 500/900 symbols with periodic spikes; RSS must plateau (exit 1 on growth > 3 MB) |
| `scripts/bench_history.py` | PriceHistory fill/lookup micro-benchmark |

Run with `PYTHONPATH=src;.` (Windows) or `PYTHONPATH=src:.`.

## Before / after (same machine, Windows, Python 3.12)

Live, 521 symbols, 25 s, public WS, deflate:

| Metric | Before | After (profiler on) | After (profiler off) |
|--------|--------|---------------------|----------------------|
| CPU | 10.2 % | 9.9 % | 7.6 % |
| RSS total / before history | 90.5 / 51.1 MB | 90.8 / 51.4 MB | 91.0 / 51.4 MB |
| WS frames/s | 764 | 775 | 832 |
| Engine evals/s (= live lastPrice/s) | 58.5 | 80.8 | 80.8 |
| Pipeline p50 / p95 / p99 | 0.15 / 1.01 / 2.15 ms | 0.18 / 1.19 / 1.94 ms | 0.11 / 1.52 / 7.43 ms |
| Event-loop lag max | ~0 | 16 ms (profiler + Windows timer) | 0 ms |
| raw drops / lost events | 0 / 0 | 0 / 0 | 0 / 0 |

Live numbers depend on market activity (evals/s rose because the market was busier).

Synthetic, 300 market seconds (after = range of 3 runs):

| Metric | 500 before | 500 after | 900 before | 900 after |
|--------|-----------|-----------|-----------|-----------|
| PriceHistory RAM | 32.6 MB | 32.6 MB | 58.1 MB | 58.1 MB |
| Pipeline frames/s | ~256k | 224–240k | ~252k | 213–233k |
| Engine evals/s | ~19.5k | 17.0–18.5k | — | 16.5–18.0k |
| Lookup | 1.55 µs | 1.54–1.66 µs | 1.63 µs | 1.66–1.69 µs |
| Live event p50 / p95 | 19.5 / 28.6 µs | 19.1–19.6 / 35–51 µs | — / 30.2 µs | 19.5–20.3 / 35–52 µs |

Synthetic soak (1 h market after full warmup):

| | 500 before | 500 after | 900 before | 900 after |
|--|-----------|-----------|-----------|-----------|
| RSS startup → warmup | 32.9 → 65.6 MB | 32.9 → 65.6 MB | 32.5 → 90.9 MB | 32.6 → 91.0 MB |
| Growth after warmup | 0.01 MB | 0.03 MB | flat | 0.04 MB |
| Signals | 531 | 531 | — | 539 |

The engine hot path gained one attribute read (immutable config snapshot). p50 is unchanged;
the p95 tail varies 35–51 µs between identical runs, so the difference to the single baseline
run is within run-to-run noise. For scale: at ~800 frames/s live, the budget is ~1.25 ms per
frame, and the engine spends ~20 µs per live event.

## Commercial mode (multi-user)

`scripts/benchmark_commercial.py --symbols 500 --seconds 300 --users 1000 --groups 250`
(`PYTHONPATH=scripts;src`): admin `SignalEngine` alone vs admin + `MultiConfigEngine` on the same
`PriceHistory`, users spread over distinct configs (groups), fan-out through `UserDispatcher`.

| Metric | 500 sym / 1000 users / 253 groups | 900 sym / 3000 users / 763 groups |
|--------|-----------------------------------|-----------------------------------|
| Live event p50 / p99, admin only | 18.6 / 39.2 µs | — |
| Live event p50 / p99, admin + commercial | 38.1 / 116.8 µs | p99 overhead ≈ 77 µs |
| Commercial eval p99 | 67.7 µs | — |
| Fan-out cost | ~11 µs per delivery | — |
| Admin signals | identical to admin-only run | identical |
| RSS | 71 MB | — |

Cost scales with the number of distinct configs (groups), not users: identical configs share one
evaluation. The real limit is Telegram (~25 msg/s globally, ≥1 s per chat): synthetic spikes every
20 s produce more deliveries than that, so they queue (one signal per message) and the
oldest are dropped after `USER_SIGNAL_MAX_AGE_SEC`. Admin signals are never affected.

## Decisions

- **PriceHistory** (`array('q'/'d')` rings, ~64 KB/symbol for 3840 s) was already optimal: not changed.
- **WS compression**: `none` costs ~15–20 % less CPU per frame (≈1–2 % of a core) but 5.6× more
  traffic (~12.8 GB/day vs ~2.3 GB/day at 760 frames/s). Default stays `deflate`;
  `WS_COMPRESSION=none` is opt-in for CPU-bound hosts with cheap traffic.
- **WS max_queue** stays 1024 (worst case ~1.2 MB, never blocked in measurements).
- **Bounded state**: dedup and `RecentSignals` forget delisted symbols and sweep expired keys;
  trading instrument cache capped at 256; background notify tasks are tracked and cancelled.
- **SQL**: `signal_stats` uses one `GROUP BY` instead of a query per type.
- No manual `gc.collect()`, no Redis/Kafka/Celery, Decimal kept in trading.

## Remaining bottlenecks

- At real load the process is I/O-idle: >90 % of wall time is in `GetQueuedCompletionStatus`
  (event-loop wait). The top CPU items are TLS reads, websockets frame parsing and zlib
  decompress — all in libraries.
- Process RSS is dominated by the interpreter + libraries (~51 MB) and PriceHistory (~64 KB/symbol).
  Shrinking history further would require shortening the 3600 s maximum window.
