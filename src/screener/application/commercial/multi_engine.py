"""Commercial rule evaluation for many user configs on the shared price history.

Users with identical effective settings share one *group* (``config_hash``).
Per event the engine does one history lookup per distinct window, computes the
change once, and finds triggered groups by bisecting sorted thresholds, so the
cost is O(buckets * log(groups)) rather than O(users).

Semantics per group are exactly those of ``SignalEngine`` with that group's
settings (same comparators, same ``SignalDeduplicator``); a dedup call is only
skipped when it is a guaranteed no-op (not triggered and no state).
"""

from __future__ import annotations

import logging
from bisect import bisect_right
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from screener.application.dedup import DedupKey, SignalDeduplicator
from screener.application.metrics import metrics
from screener.application.price_history import PriceHistoryManager, WindowLookup
from screener.config import ScreenerSettings
from screener.domain.enums import Direction, HistoryQuality, PriceEventKind, SignalType
from screener.domain.models import PriceEvent, RuleMatch
from screener.domain.rules import DIRECTION_OF

logger = logging.getLogger(__name__)

GroupMatchHandler = Callable[[PriceEvent, str, list[RuleMatch]], None]

_TYPE_ORDER = (SignalType.PUMP, SignalType.DROP, SignalType.DUMP)


def rules_of(settings: ScreenerSettings) -> dict[SignalType, tuple[float, int]]:
    """Enabled rules as ``type -> (threshold_pct, interval_sec)``."""
    s = settings
    candidates = (
        (s.enable_long, SignalType.PUMP, s.long_threshold_pct, s.long_interval_sec),
        (s.enable_short, SignalType.DROP, s.short_threshold_pct, s.short_interval_sec),
        (s.enable_dump, SignalType.DUMP, s.dump_threshold_pct, s.dump_interval_sec),
    )
    return {t: (pct, interval) for enabled, t, pct, interval in candidates if enabled}


@dataclass(slots=True)
class _Group:
    key: str
    settings: ScreenerSettings
    dedup: SignalDeduplicator


@dataclass(slots=True, frozen=True)
class _Bucket:
    signal_type: SignalType
    interval_sec: int
    up: bool
    thresholds: tuple[float, ...]  # ascending
    keys: tuple[str, ...]  # aligned with thresholds
    threshold_of: Mapping[str, float]


@dataclass(slots=True, frozen=True)
class _Compiled:
    system: ScreenerSettings
    groups: Mapping[str, _Group]
    buckets: tuple[_Bucket, ...]
    windows: tuple[int, ...]


def _new_dedup(settings: ScreenerSettings) -> SignalDeduplicator:
    return SignalDeduplicator(
        cooldown_sec=settings.cooldown_sec,
        escalate_step_pct=settings.escalate_step_pct,
        reset_ratio=settings.reset_ratio,
        reset_hold_sec=settings.reset_hold_sec,
    )


class MultiConfigEngine:
    def __init__(
        self,
        history: PriceHistoryManager,
        system: ScreenerSettings,
        *,
        on_matches: GroupMatchHandler | None = None,
    ) -> None:
        self._history = history
        self.on_matches = on_matches
        # (symbol, type, interval) -> group keys holding dedup state there.
        self._active: dict[tuple[str, SignalType, int], set[str]] = {}
        self._c = _Compiled(system=system, groups={}, buckets=(), windows=())

    @property
    def system(self) -> ScreenerSettings:
        return self._c.system

    @property
    def group_count(self) -> int:
        return len(self._c.groups)

    @property
    def active_states(self) -> int:
        return sum(len(v) for v in self._active.values())

    @property
    def max_interval_sec(self) -> int:
        return max(self._c.windows, default=0)

    def group_keys(self) -> set[str]:
        return set(self._c.groups)

    def set_groups(
        self, groups: Mapping[str, ScreenerSettings], system: ScreenerSettings | None = None
    ) -> None:
        """Swap in a new compiled index; groups that survive keep their dedup state."""
        old = self._c
        new_groups: dict[str, _Group] = {}
        for key, settings in groups.items():
            prev = old.groups.get(key)
            new_groups[key] = (
                prev if prev is not None else _Group(key, settings, _new_dedup(settings))
            )
        per_bucket: dict[tuple[SignalType, int], list[tuple[float, str]]] = {}
        for key, group in new_groups.items():
            for signal_type, (pct, interval) in rules_of(group.settings).items():
                per_bucket.setdefault((signal_type, interval), []).append((pct, key))
        buckets: list[_Bucket] = []
        for (signal_type, interval), items in sorted(
            per_bucket.items(), key=lambda kv: (_TYPE_ORDER.index(kv[0][0]), kv[0][1])
        ):
            items.sort()
            buckets.append(
                _Bucket(
                    signal_type=signal_type,
                    interval_sec=interval,
                    up=DIRECTION_OF[signal_type] is Direction.UP,
                    thresholds=tuple(p for p, _ in items),
                    keys=tuple(k for _, k in items),
                    threshold_of={k: p for p, k in items},
                )
            )
        removed = set(old.groups) - set(new_groups)
        if removed:
            for akey in list(self._active):
                keys = self._active[akey]
                keys -= removed
                if not keys:
                    del self._active[akey]
        self._c = _Compiled(
            system=system or old.system,
            groups=new_groups,
            buckets=tuple(buckets),
            windows=tuple(sorted({b.interval_sec for b in buckets})),
        )
        metrics.commercial_groups = len(new_groups)

    def clear_symbol(self, symbol: str) -> None:
        for group in self._c.groups.values():
            group.dedup.clear_symbol(symbol)
        for akey in [k for k in self._active if k[0] == symbol]:
            del self._active[akey]

    def evaluate(self, event: PriceEvent) -> int:
        c = self._c
        if event.kind is not PriceEventKind.LIVE or not c.buckets:
            return 0
        system = c.system
        symbol = event.symbol
        if symbol in system.exclude_symbols:
            return 0
        metrics.commercial_evaluations_total += 1

        now_ms = event.exchange_ts_ms
        price = event.last_price
        lookups: dict[int, tuple[WindowLookup, float] | None] = {}
        for window in c.windows:
            lookup = self._history.lookup(symbol, now_ms=now_ms, interval_sec=window)
            if lookup is None or lookup.price <= 0:
                lookups[window] = None
                continue
            if lookup.quality is not HistoryQuality.LIVE and not system.allow_mixed_history:
                lookups[window] = None
                continue
            lookups[window] = (lookup, (price - lookup.price) / lookup.price * 100.0)

        turnover = event.turnover_24h
        found: dict[str, list[RuleMatch]] = {}
        active = self._active
        groups = c.groups
        for bucket in c.buckets:
            entry = lookups[bucket.interval_sec]
            if entry is None:
                continue
            lookup, change = entry
            # UP: change >= t;  DOWN: change <= -t  <=>  -change >= t.
            n = bisect_right(bucket.thresholds, change if bucket.up else -change)
            akey = (symbol, bucket.signal_type, bucket.interval_sec)
            states = active.get(akey)
            if n == 0 and not states:
                continue
            triggered_keys = bucket.keys[:n]
            for key in triggered_keys:
                group = groups[key]
                triggered = turnover >= group.settings.min_turnover_24h_usdt
                if not triggered and (states is None or key not in states):
                    continue
                self._step(group, bucket, akey, lookup, change, price, now_ms, triggered, found)
            if states:
                hit = set(triggered_keys) if n else ()
                for key in list(states):
                    if key in hit:
                        continue
                    self._step(
                        groups[key], bucket, akey, lookup, change, price, now_ms, False, found
                    )

        if not found:
            return 0
        total = 0
        for key, matches in found.items():
            total += len(matches)
            if self.on_matches is not None:
                self.on_matches(event, key, matches)
        metrics.commercial_rule_matches_total += total
        return total

    def _step(
        self,
        group: _Group,
        bucket: _Bucket,
        akey: tuple[str, SignalType, int],
        lookup: WindowLookup,
        change: float,
        price: float,
        now_ms: int,
        triggered: bool,
        found: dict[str, list[RuleMatch]],
    ) -> None:
        threshold = bucket.threshold_of[group.key]
        symbol, signal_type, interval = akey
        decision = group.dedup.evaluate(
            DedupKey(symbol, signal_type, interval),
            change_pct=change,
            threshold_pct=threshold,
            now_ms=now_ms,
            triggered=triggered,
        )
        if group.dedup.has_state(symbol, signal_type, interval):
            self._active.setdefault(akey, set()).add(group.key)
        else:
            keys = self._active.get(akey)
            if keys is not None:
                keys.discard(group.key)
                if not keys:
                    del self._active[akey]
        if decision.should_notify:
            found.setdefault(group.key, []).append(
                RuleMatch(
                    signal_type=signal_type,
                    interval_sec=interval,
                    threshold_pct=threshold,
                    change_pct=change,
                    prev_price=lookup.price,
                    price=price,
                    detected_ts_ms=now_ms,
                    history_quality=lookup.quality,
                    level=decision.level,
                )
            )
