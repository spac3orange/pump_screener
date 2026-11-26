"""Evaluate every monitoring profile in parallel on shared price history.

Group key is ``str(profile.id)`` so identical configs still emit separate
signals (one Telegram alert per profile, with that profile's name).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence

from screener.application.commercial.multi_engine import MultiConfigEngine
from screener.application.correlator import SignalCorrelator
from screener.application.price_history import PriceHistoryManager, history_capacity_for
from screener.application.profiles.service import settings_from_version
from screener.config import ScreenerSettings
from screener.domain.models import PriceEvent, RuleMatch, SignalContext
from screener.domain.profiles import MonitoringProfile, ProfileValidationError

logger = logging.getLogger(__name__)

EnsureCapacity = Callable[[int], None]


class ProfileMonitor:
    """Admin multi-profile evaluator: MultiConfigEngine → SignalCorrelator."""

    def __init__(
        self,
        history: PriceHistoryManager,
        correlator: SignalCorrelator,
        system: ScreenerSettings,
        *,
        ensure_capacity: EnsureCapacity | None = None,
    ) -> None:
        self._history = history
        self._correlator = correlator
        self._ensure_capacity = ensure_capacity
        self._contexts: dict[str, SignalContext] = {}
        self._settings: dict[str, ScreenerSettings] = {}
        self._engine = MultiConfigEngine(
            history,
            _without_excludes(system),
            on_matches=self._on_matches,
            track_commercial_metrics=False,
        )

    @property
    def group_count(self) -> int:
        return self._engine.group_count

    @property
    def max_interval_sec(self) -> int:
        return self._engine.max_interval_sec

    def settings_for(self, profile_id: str) -> ScreenerSettings | None:
        return self._settings.get(profile_id)

    def capped_symbols(self) -> set[str]:
        out: set[str] = set()
        for settings in self._settings.values():
            out.update(settings.symbol_daily_limits)
        return out

    def rebuild(
        self, profiles: Sequence[MonitoringProfile], system: ScreenerSettings | None = None
    ) -> None:
        groups: dict[str, ScreenerSettings] = {}
        contexts: dict[str, SignalContext] = {}
        settings_map: dict[str, ScreenerSettings] = {}
        max_interval = 0
        for profile in profiles:
            try:
                settings = settings_from_version(profile.current)
            except ProfileValidationError:
                logger.error(
                    "Monitoring profile %s has invalid config; skipped in multi-monitor",
                    profile.name,
                )
                continue
            key = str(profile.id)
            groups[key] = settings
            settings_map[key] = settings
            contexts[key] = SignalContext(
                rule_version=settings.rule_version(),
                profile_id=profile.id,
                profile_version_id=profile.current.id,
                profile_name=profile.name,
                profile_version_no=profile.current.version_no,
            )
            max_interval = max(
                max_interval,
                settings.long_interval_sec,
                settings.short_interval_sec,
                settings.dump_interval_sec,
            )
        self._contexts = contexts
        self._settings = settings_map
        # System exclude is empty: each group applies its own exclude_symbols.
        base = _without_excludes(system) if system is not None else ScreenerSettings()
        self._engine.set_groups(groups, base)
        if self._ensure_capacity is not None and max_interval > 0:
            self._ensure_capacity(history_capacity_for(max_interval))

    def evaluate(self, event: PriceEvent) -> int:
        return self._engine.evaluate(event)

    def _on_matches(self, event: PriceEvent, group_key: str, matches: list[RuleMatch]) -> None:
        context = self._contexts.get(group_key)
        self._correlator.add(event, matches, context=context)


def _without_excludes(settings: ScreenerSettings) -> ScreenerSettings:
    if not settings.exclude_symbols:
        return settings
    return settings.model_copy(update={"exclude_symbols": set()})
