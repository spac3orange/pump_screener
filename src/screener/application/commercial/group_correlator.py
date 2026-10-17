from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from screener.application.metrics import metrics
from screener.domain.commercial import GroupSignal
from screener.domain.enums import Direction, SignalType
from screener.domain.exchanges import BYBIT
from screener.domain.models import PriceEvent, RuleMatch
from screener.domain.rules import DIRECTION_OF, SEVERITY

logger = logging.getLogger(__name__)

GroupSignalHandler = Callable[[GroupSignal], None]
_Key = tuple[str, str, Direction]


@dataclass
class _Pending:
    matches: dict[tuple[SignalType, int], RuleMatch] = field(default_factory=dict)
    turnover_24h: float = 0.0
    handle: asyncio.TimerHandle | None = None


class GroupCorrelator:
    """``SignalCorrelator`` semantics per ``(group, symbol, direction)``."""

    def __init__(
        self,
        *,
        window_ms: int,
        on_signal: GroupSignalHandler | None = None,
        exchange: str = BYBIT,
    ) -> None:
        self.window_ms = window_ms
        self.on_signal = on_signal
        self.exchange = exchange
        self._pending: dict[_Key, _Pending] = {}

    @property
    def pending(self) -> int:
        return len(self._pending)

    def add(self, event: PriceEvent, group_key: str, matches: list[RuleMatch]) -> None:
        for match in matches:
            key = (group_key, event.symbol, DIRECTION_OF[match.signal_type])
            pending = self._pending.get(key)
            if pending is None:
                pending = _Pending()
                self._pending[key] = pending
                if self.window_ms > 0:
                    loop = asyncio.get_running_loop()
                    pending.handle = loop.call_later(self.window_ms / 1000, self._flush, key)
            pending.matches[(match.signal_type, match.interval_sec)] = match
            pending.turnover_24h = event.turnover_24h
        if self.window_ms <= 0:
            self.flush_all()

    def flush_all(self) -> None:
        for key in list(self._pending):
            self._flush(key)

    def _flush(self, key: _Key) -> None:
        pending = self._pending.pop(key, None)
        if pending is None or not pending.matches:
            return
        if pending.handle is not None:
            pending.handle.cancel()
        matches = sorted(pending.matches.values(), key=lambda m: m.interval_sec)
        primary = max(matches, key=lambda m: (SEVERITY[m.signal_type], abs(m.change_pct)))
        signal = GroupSignal(
            group_key=key[0],
            symbol=key[1],
            direction=key[2],
            primary=primary,
            matches=tuple(matches),
            detected_at=datetime.fromtimestamp(primary.detected_ts_ms / 1000, tz=UTC),
            turnover_24h=pending.turnover_24h,
            exchange=self.exchange,
        )
        metrics.commercial_signals_total += 1
        if self.on_signal is None:
            return
        try:
            self.on_signal(signal)
        except Exception:
            metrics.commercial_engine_errors_total += 1
            logger.exception("Commercial signal fan-out failed for %s", key[1])
