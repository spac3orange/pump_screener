"""Commercial load benchmark: admin pipeline vs admin + multi-user engine (no WS/DB/Telegram).

Builds ``--users`` subscribers spread over ~``--groups`` distinct configs, runs the
synthetic Bybit stream from ``perfutil.Workload`` (with periodic +60% spikes so
rules fire), and reports per-live-event latency for the admin engine alone and
with the commercial engine on the same history, plus fan-out/delivery CPU cost.

Usage: python scripts/benchmark_commercial.py [--symbols 500] [--users 1000] [--groups 300]
"""

from __future__ import annotations

import argparse
import asyncio
import random
import time
from datetime import UTC, datetime

from perfutil import Workload, percentiles_us, rss_mb

from screener.application.clock import FakeClock
from screener.application.commercial.dispatcher import UserDispatcher
from screener.application.commercial.group_correlator import GroupCorrelator
from screener.application.commercial.multi_engine import MultiConfigEngine
from screener.application.commercial.registry import ActiveUserRegistry, UserEntry
from screener.application.metrics import metrics
from screener.config import CommercialSettings, ScreenerSettings
from screener.domain.commercial import UserDetectionConfig, UserRole

PCTS = (3.0, 5.0, 7.0, 10.0, 15.0, 20.0, 30.0, 40.0)
MINUTES = (1, 3, 5, 10, 15, 30, 60)
TURNOVERS = (0.0, 100_000.0, 500_000.0, 1_000_000.0)
ACCESS_UNTIL = datetime(2100, 1, 1, tzinfo=UTC)


def _configs(n: int, rnd: random.Random) -> list[UserDetectionConfig]:
    seen: dict[tuple, UserDetectionConfig] = {}
    while len(seen) < n:
        cfg = UserDetectionConfig(
            enable_long=rnd.random() < 0.9,
            long_threshold_pct=rnd.choice(PCTS),
            long_interval_sec=rnd.choice(MINUTES) * 60,
            enable_short=rnd.random() < 0.8,
            short_threshold_pct=rnd.choice(PCTS),
            short_interval_sec=rnd.choice(MINUTES) * 60,
            enable_dump=rnd.random() < 0.6,
            dump_threshold_pct=rnd.choice(PCTS[3:]),
            dump_interval_sec=rnd.choice(MINUTES) * 60,
            min_turnover_24h_usdt=rnd.choice(TURNOVERS),
        )
        seen[tuple(cfg.model_dump().values())] = cfg
    return list(seen.values())


class _Combined:
    """Stands in for ``SignalEngine`` in the workload: admin first, then commercial."""

    def __init__(self, admin, commercial: MultiConfigEngine | None) -> None:
        self.admin = admin
        self.commercial = commercial
        self.commercial_samples: list[float] = []

    def evaluate(self, event) -> None:
        self.admin.evaluate(event)
        if self.commercial is not None:
            start = time.perf_counter()
            self.commercial.evaluate(event)
            self.commercial_samples.append(time.perf_counter() - start)


class _NullWriter:
    def enqueue_signal(self, _signal) -> None:
        return None

    def enqueue_record(self, _record) -> None:
        return None


def _run(symbols: int, seconds: int, *, users: int, groups: int, seed: int) -> dict:
    metrics.__init__()
    template = ScreenerSettings(min_turnover_24h_usdt=0, correlation_window_ms=0)
    wl = Workload(symbols, seed=seed, spike_every_sec=20, settings=template)
    commercial = None
    dispatcher = None
    registry = None
    # Market time of the synthetic stream, so signal age checks see realistic ages.
    clock = FakeClock(wl.sec * 1000)
    if users:
        rnd = random.Random(seed)
        configs = _configs(groups, rnd)
        weights = [1.0 / (i + 1) ** 0.8 for i in range(len(configs))]
        registry = ActiveUserRegistry()
        items = []
        for uid in range(1, users + 1):
            cfg = rnd.choices(configs, weights)[0]
            settings = cfg.to_settings(template)
            entry = UserEntry(uid, uid, UserRole.USER, ACCESS_UNTIL, settings.config_hash())
            items.append((entry, settings))
        commercial = MultiConfigEngine(wl.history, template)
        correlator = GroupCorrelator(window_ms=0)
        commercial.on_matches = correlator.add
        registry.on_groups_changed = commercial.set_groups
        registry.replace_all(items)
        sent = 0

        async def send(_chat_id: int, _text: str) -> None:
            nonlocal sent
            sent += 1

        dispatcher = UserDispatcher(
            registry,
            _NullWriter(),  # type: ignore[arg-type]
            clock,
            CommercialSettings(_env_file=None, user_signal_max_age_sec=3600),  # type: ignore[call-arg]
            format_signal=lambda s, n: f"{s.symbol} {s.change_pct:.2f}% #{n}",
            send=send,
        )
        correlator.on_signal = dispatcher.on_group_signal
    combined = _Combined(wl.engine, commercial)
    wl.engine = combined  # type: ignore[assignment]
    wl.snapshot_all()
    wl.prefill_history()
    cpu0 = time.process_time()
    for _ in range(seconds):
        wl.step_second()
    cpu = time.process_time() - cpu0

    result = {
        "live_event_us": percentiles_us(wl.live_samples),
        "frames": wl.frames,
        "admin_signals": wl.signals,
        "cpu_s": round(cpu, 2),
        "rss_mb": round(rss_mb(), 1),
    }
    if commercial is not None and dispatcher is not None and registry is not None:
        clock.set(wl.sec * 1000)
        fan0 = time.perf_counter()
        queued = 0
        while dispatcher._fanout:
            queued += dispatcher.fanout_once()
        fan = time.perf_counter() - fan0

        async def drain() -> int:
            n = 0
            while await dispatcher.send_next():
                n += 1
            return n

        send0 = time.perf_counter()
        batches = asyncio.run(drain())
        send_s = time.perf_counter() - send0
        result.update(
            groups=commercial.group_count,
            users=registry.user_count,
            commercial_eval_us=percentiles_us(combined.commercial_samples),
            group_signals=metrics.commercial_signals_total,
            deliveries_queued=queued,
            messages=batches,
            fanout_us_per_delivery=round(fan / max(queued, 1) * 1e6, 2),
            send_us_per_message=round(send_s / max(batches, 1) * 1e6, 2),
            active_dedup_states=commercial.active_states,
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", type=int, default=500)
    parser.add_argument("--seconds", type=int, default=120)
    parser.add_argument("--users", type=int, default=1000)
    parser.add_argument("--groups", type=int, default=300)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument(
        "--max-p99-overhead-us",
        type=float,
        default=0.0,
        help="fail (exit 1) if commercial adds more than this to live-event p99",
    )
    args = parser.parse_args()
    base = _run(args.symbols, args.seconds, users=0, groups=0, seed=args.seed)
    full = _run(args.symbols, args.seconds, users=args.users, groups=args.groups, seed=args.seed)
    print(f"symbols={args.symbols} market_seconds={args.seconds} frames={base['frames']}")
    print(f"  admin only:        live_event_us={base['live_event_us']} cpu_s={base['cpu_s']}")
    print(
        f"  admin+commercial:  live_event_us={full['live_event_us']} cpu_s={full['cpu_s']} "
        f"rss_mb={full['rss_mb']}"
    )
    print(
        f"  commercial: users={full['users']} groups={full['groups']} "
        f"eval_us={full['commercial_eval_us']} group_signals={full['group_signals']} "
        f"dedup_states={full['active_dedup_states']}"
    )
    print(
        f"  delivery: queued={full['deliveries_queued']} messages={full['messages']} "
        f"fanout_us_per_delivery={full['fanout_us_per_delivery']} "
        f"send_us_per_message={full['send_us_per_message']}"
    )
    print(f"  admin signals equal: {base['admin_signals'] == full['admin_signals']}")
    overhead = full["live_event_us"]["p99"] - base["live_event_us"]["p99"]
    print(f"  p99 overhead_us={overhead:.2f}")
    if base["admin_signals"] != full["admin_signals"]:
        return 1
    if args.max_p99_overhead_us and overhead > args.max_p99_overhead_us:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
