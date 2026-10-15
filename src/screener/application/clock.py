from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def time_ms(self) -> int: ...

    def now(self) -> datetime: ...


class SystemClock:
    def time_ms(self) -> int:
        return int(time.time() * 1000)

    def now(self) -> datetime:
        return datetime.now(UTC)


class FakeClock:
    def __init__(self, start_ms: int = 0) -> None:
        self._now = start_ms

    def time_ms(self) -> int:
        return self._now

    def now(self) -> datetime:
        return datetime.fromtimestamp(self._now / 1000, tz=UTC)

    def advance(self, ms: int) -> None:
        self._now += ms

    def set(self, ms: int) -> None:
        self._now = ms
