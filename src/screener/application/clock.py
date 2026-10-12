from __future__ import annotations

import time


class SystemClock:
    def time_ms(self) -> int:
        return int(time.time() * 1000)


class FakeClock:
    def __init__(self, start_ms: int = 0) -> None:
        self._now = start_ms

    def time_ms(self) -> int:
        return self._now

    def advance(self, ms: int) -> None:
        self._now += ms

    def set(self, ms: int) -> None:
        self._now = ms
