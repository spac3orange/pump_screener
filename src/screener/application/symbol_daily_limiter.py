"""Per-symbol signal caps for the current UTC calendar day."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime


def utc_today(now: datetime | None = None) -> date:
    instant = now if now is not None else datetime.now(UTC)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=UTC)
    return instant.astimezone(UTC).date()


def recent_key(symbol: str, exchange: str = "bybit") -> str:
    return f"{exchange}:{symbol}"


class SymbolDailyLimiter:
    """In-memory counters keyed by ``exchange:symbol`` for one UTC calendar day."""

    def __init__(self) -> None:
        self._day: date = utc_today()
        self._counts: dict[str, int] = defaultdict(int)

    def _roll(self, now: datetime | None = None) -> None:
        today = utc_today(now)
        if today != self._day:
            self._day = today
            self._counts.clear()

    def count(self, symbol: str, exchange: str = "bybit", *, now: datetime | None = None) -> int:
        self._roll(now)
        return self._counts.get(recent_key(symbol, exchange), 0)

    def allow(
        self,
        symbol: str,
        *,
        exchange: str = "bybit",
        limit: int | None,
        now: datetime | None = None,
    ) -> bool:
        """True when the signal may be dispatched (no limit, or under the cap)."""
        if limit is None:
            return True
        if limit < 1:
            return False
        return self.count(symbol, exchange, now=now) < limit

    def record(
        self, symbol: str, exchange: str = "bybit", *, now: datetime | None = None
    ) -> None:
        self._roll(now)
        self._counts[recent_key(symbol, exchange)] += 1

    def seed(
        self,
        counts: dict[tuple[str, str], int],
        *,
        now: datetime | None = None,
    ) -> None:
        """Replace today's counters from durable storage (exchange, symbol) -> count."""
        self._roll(now)
        self._counts.clear()
        for (exchange, symbol), value in counts.items():
            if value > 0:
                self._counts[recent_key(symbol, exchange)] = int(value)

    def clear(self) -> None:
        self._counts.clear()
        self._day = utc_today()
