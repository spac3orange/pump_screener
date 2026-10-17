from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from screener.application.metrics import metrics
from screener.domain.enums import Direction, SignalType
from screener.domain.exchanges import BYBIT
from screener.domain.models import MarketSignal, PriceEvent, RuleMatch, SignalContext
from screener.domain.rules import DIRECTION_OF, SEVERITY
from screener.logging import log

logger = logging.getLogger(__name__)

SignalHandler = Callable[[MarketSignal], None]


@dataclass
class _Group:
    matches: dict[tuple[SignalType, int], RuleMatch] = field(default_factory=dict)
    turnover_24h: float = 0.0
    volume_24h: float = 0.0
    handle: asyncio.TimerHandle | None = None
    context: SignalContext | None = None


def _primary(matches: list[RuleMatch]) -> RuleMatch:
    return max(matches, key=lambda m: (SEVERITY[m.signal_type], abs(m.change_pct)))


class SignalCorrelator:
    """Merges rule matches of one ``symbol + direction`` move into one MarketSignal.

    The first match opens a short window; matches arriving within it (for
    example DROP then DUMP on consecutive price events) join the same signal.
    Moves further apart than the window stay independent.
    """

    def __init__(
        self,
        *,
        window_ms: int,
        rule_version: Callable[[], str],
        on_signal: SignalHandler | None = None,
        exchange: str = BYBIT,
    ) -> None:
        self.window_ms = window_ms
        self._rule_version = rule_version
        self.on_signal = on_signal
        self.exchange = exchange
        self._groups: dict[tuple[str, Direction], _Group] = {}

    def add(
        self,
        event: PriceEvent,
        matches: list[RuleMatch],
        context: SignalContext | None = None,
    ) -> None:
        for match in matches:
            key = (event.symbol, DIRECTION_OF[match.signal_type])
            group = self._groups.get(key)
            if group is not None and group.context != context:
                # Matches from different monitoring configs never share one signal.
                self._flush(key)
                group = None
            if group is None:
                group = _Group(context=context)
                self._groups[key] = group
                if self.window_ms > 0:
                    loop = asyncio.get_running_loop()
                    group.handle = loop.call_later(self.window_ms / 1000, self._flush, key)
            group.matches[(match.signal_type, match.interval_sec)] = match
            group.turnover_24h = event.turnover_24h
            group.volume_24h = event.volume_24h
        if self.window_ms <= 0:
            self.flush_all()

    def _flush(self, key: tuple[str, Direction]) -> None:
        group = self._groups.pop(key, None)
        if group is None or not group.matches:
            return
        if group.handle is not None:
            group.handle.cancel()
        matches = sorted(group.matches.values(), key=lambda m: m.interval_sec)
        primary = _primary(matches)
        ctx = group.context or SignalContext(rule_version=self._rule_version())
        signal = MarketSignal(
            symbol=key[0],
            direction=key[1],
            primary=primary,
            matches=tuple(matches),
            detected_at=datetime.fromtimestamp(primary.detected_ts_ms / 1000, tz=UTC),
            rule_version=ctx.rule_version,
            turnover_24h=group.turnover_24h,
            volume_24h=group.volume_24h,
            exchange=self.exchange,
            monitoring_profile_id=ctx.profile_id,
            monitoring_profile_version_id=ctx.profile_version_id,
            monitoring_profile_name=ctx.profile_name,
            monitoring_profile_version_no=ctx.profile_version_no,
        )
        metrics.record_signal()
        if len(matches) > 1:
            metrics.signals_correlated_total += len(matches) - 1
        types = ",".join(m.signal_type.value for m in matches)
        log(
            logger,
            logging.INFO,
            f"MarketSignal {signal.symbol} primary={primary.signal_type.value} "
            f"change={primary.change_pct:.2f}% window={primary.interval_sec}s "
            f"matches={len(matches)} [{types}] id={signal.id}",
            component="correlator",
            event="signal_emitted",
            symbol=signal.symbol,
            signal_id=str(signal.id),
            signal_type=primary.signal_type.value,
            count=len(matches),
        )
        if self.on_signal is None:
            return
        try:
            self.on_signal(signal)
        except Exception:
            metrics.record_error()
            log(
                logger,
                logging.ERROR,
                f"Signal dispatch failed for {key[0]}",
                component="correlator",
                event="dispatch_failed",
                symbol=key[0],
                signal_id=str(signal.id),
                exc_info=True,
            )
    def flush_all(self) -> None:
        for key in list(self._groups):
            self._flush(key)

    @property
    def pending(self) -> int:
        return len(self._groups)
