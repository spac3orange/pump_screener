from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable


class UserRateLimiter:
    """Per-user token bucket for incoming bot updates; memory bounded by ``max_users``."""

    def __init__(
        self,
        *,
        rate_per_sec: float,
        burst: int,
        max_users: int = 50_000,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._rate = rate_per_sec
        self._burst = float(burst)
        self._max = max_users
        self._mono = monotonic
        self._buckets: OrderedDict[int, tuple[float, float]] = OrderedDict()

    def allow(self, user_id: int) -> bool:
        now = self._mono()
        tokens, last = self._buckets.pop(user_id, (self._burst, now))
        tokens = min(self._burst, tokens + (now - last) * self._rate)
        allowed = tokens >= 1.0
        if allowed:
            tokens -= 1.0
        self._buckets[user_id] = (tokens, now)
        if len(self._buckets) > self._max:
            self._buckets.popitem(last=False)
        return allowed
