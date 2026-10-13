from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from screener.application.dedup import DedupKey, SignalDeduplicator
from screener.application.metrics import metrics
from screener.application.price_history import PriceHistoryManager, WindowLookup
from screener.config import ScreenerSettings
from screener.domain.enums import Direction, HistoryQuality, PriceEventKind, SignalType
from screener.domain.models import PriceEvent, RuleMatch
from screener.domain.rules import DIRECTION_OF

logger = logging.getLogger(__name__)

MatchHandler = Callable[[PriceEvent, list[RuleMatch]], None]


@dataclass(slots=True, frozen=True)
class _Rule:
    signal_type: SignalType
    threshold_pct: float
    interval_sec: int
    direction: Direction


def _rules_from(settings: ScreenerSettings) -> tuple[_Rule, ...]:
    s = settings
    candidates = (
        (s.enable_long, SignalType.PUMP, s.long_threshold_pct, s.long_interval_sec),
        (s.enable_short, SignalType.DROP, s.short_threshold_pct, s.short_interval_sec),
        (s.enable_dump, SignalType.DUMP, s.dump_threshold_pct, s.dump_interval_sec),
    )
    return tuple(
        _Rule(signal_type, threshold, interval, DIRECTION_OF[signal_type])
        for enabled, signal_type, threshold, interval in candidates
        if enabled
    )


class SignalEngine:
    """Evaluates price rules on live lastPrice events.

    One history lookup per unique window per event; rules only compare the
    precomputed change against their threshold.
    """

    def __init__(
        self,
        history: PriceHistoryManager,
        settings: ScreenerSettings,
        *,
        dedup: SignalDeduplicator | None = None,
        on_matches: MatchHandler | None = None,
    ) -> None:
        self._history = history
        self._dedup = dedup or SignalDeduplicator()
        self.on_matches = on_matches
        self.update_settings(settings)

    @property
    def settings(self) -> ScreenerSettings:
        return self._settings

    @property
    def rule_version(self) -> str:
        return self._rule_version

    def update_settings(self, settings: ScreenerSettings) -> None:
        self._settings = settings
        self._rules = _rules_from(settings)
        self._windows = tuple(sorted({rule.interval_sec for rule in self._rules}))
        self._rule_version = settings.rule_version()
        self._dedup.configure(
            cooldown_sec=settings.cooldown_sec,
            escalate_step_pct=settings.escalate_step_pct,
            reset_ratio=settings.reset_ratio,
            reset_hold_sec=settings.reset_hold_sec,
        )

    def evaluate(self, event: PriceEvent) -> list[RuleMatch]:
        if event.kind is not PriceEventKind.LIVE or not self._rules:
            return []
        settings = self._settings
        if event.symbol in settings.exclude_symbols:
            return []
        metrics.engine_evaluations_total += 1

        now_ms = event.exchange_ts_ms
        price = event.last_price
        lookups: dict[int, tuple[WindowLookup, float] | None] = {}
        for window in self._windows:
            lookup = self._history.lookup(event.symbol, now_ms=now_ms, interval_sec=window)
            if lookup is None or lookup.price <= 0:
                lookups[window] = None
                continue
            if lookup.quality is not HistoryQuality.LIVE and not settings.allow_mixed_history:
                lookups[window] = None
                continue
            lookups[window] = (lookup, (price - lookup.price) / lookup.price * 100.0)

        turnover_ok = event.turnover_24h >= settings.min_turnover_24h_usdt
        matches: list[RuleMatch] = []
        for rule in self._rules:
            entry = lookups[rule.interval_sec]
            if entry is None:
                continue
            lookup, change = entry
            if rule.direction is Direction.UP:
                triggered = change >= rule.threshold_pct
            else:
                triggered = change <= -rule.threshold_pct
            triggered = triggered and turnover_ok
            decision = self._dedup.evaluate(
                DedupKey(event.symbol, rule.signal_type, rule.interval_sec),
                change_pct=change,
                threshold_pct=rule.threshold_pct,
                now_ms=now_ms,
                triggered=triggered,
            )
            if not decision.should_notify:
                continue
            matches.append(
                RuleMatch(
                    signal_type=rule.signal_type,
                    interval_sec=rule.interval_sec,
                    threshold_pct=rule.threshold_pct,
                    change_pct=change,
                    prev_price=lookup.price,
                    price=price,
                    detected_ts_ms=now_ms,
                    history_quality=lookup.quality,
                    level=decision.level,
                )
            )
        if matches:
            metrics.rule_matches_total += len(matches)
            if self.on_matches is not None:
                self.on_matches(event, matches)
        return matches
