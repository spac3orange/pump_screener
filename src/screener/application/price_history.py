from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass

from screener.domain.enums import HistoryQuality, HistorySource
from screener.domain.models import HistoryPoint
from screener.domain.rules import window_tolerance_ms


@dataclass(slots=True)
class WindowLookup:
    point: HistoryPoint
    quality: HistoryQuality


class PriceHistoryManager:
    """In-memory 1 Hz price/turnover history with sliding-window lookup."""

    def __init__(self, *, maxlen: int = 3700) -> None:
        self._maxlen = maxlen
        self._data: dict[str, deque[HistoryPoint]] = defaultdict(
            lambda: deque(maxlen=self._maxlen)
        )
        self._last_store_ts: dict[str, int] = {}
        self._first_live_ts: dict[str, int] = {}

    def clear(self, symbol: str | None = None) -> None:
        if symbol is None:
            self._data.clear()
            self._last_store_ts.clear()
            self._first_live_ts.clear()
            return
        self._data.pop(symbol, None)
        self._last_store_ts.pop(symbol, None)
        self._first_live_ts.pop(symbol, None)

    def seed_point(
        self,
        symbol: str,
        *,
        ts_ms: int,
        last_price: float,
        turnover_24h: float,
        source: HistorySource,
    ) -> None:
        point = HistoryPoint(
            ts_ms=ts_ms,
            last_price=last_price,
            turnover_24h=turnover_24h,
            source=source,
        )
        buf = self._data[symbol]
        if buf and buf[-1].ts_ms == ts_ms:
            buf[-1] = point
        else:
            buf.append(point)
        if source == HistorySource.TICKER and symbol not in self._first_live_ts:
            self._first_live_ts[symbol] = ts_ms

    def add_tick(
        self,
        symbol: str,
        *,
        ts_ms: int,
        last_price: float,
        turnover_24h: float,
        source: HistorySource = HistorySource.TICKER,
    ) -> bool:
        """Store at most 1 point per second. Returns True if stored."""
        last = self._last_store_ts.get(symbol)
        if last is not None and ts_ms - last < 1000:
            # Update in-place latest second bucket.
            buf = self._data[symbol]
            if buf and buf[-1].ts_ms // 1000 == ts_ms // 1000:
                buf[-1] = HistoryPoint(ts_ms, last_price, turnover_24h, source)
                if source == HistorySource.TICKER and symbol not in self._first_live_ts:
                    self._first_live_ts[symbol] = ts_ms
                return True
            return False

        self._last_store_ts[symbol] = ts_ms
        self.seed_point(
            symbol,
            ts_ms=ts_ms,
            last_price=last_price,
            turnover_24h=turnover_24h,
            source=source,
        )
        return True

    def latest(self, symbol: str) -> HistoryPoint | None:
        buf = self._data.get(symbol)
        if not buf:
            return None
        return buf[-1]

    def live_age_ms(self, symbol: str, now_ms: int) -> int | None:
        first = self._first_live_ts.get(symbol)
        if first is None:
            return None
        return max(0, now_ms - first)

    def history_quality(self, symbol: str, interval_sec: int, now_ms: int) -> HistoryQuality:
        age = self.live_age_ms(symbol, now_ms)
        if age is None:
            return HistoryQuality.KLINE
        if age >= interval_sec * 1000:
            return HistoryQuality.LIVE
        return HistoryQuality.MIXED

    def lookup(
        self,
        symbol: str,
        *,
        now_ms: int,
        interval_sec: int,
    ) -> WindowLookup | None:
        buf = self._data.get(symbol)
        if not buf:
            return None
        target = now_ms - interval_sec * 1000
        tol = window_tolerance_ms(interval_sec)
        # Find latest point with ts <= target within tolerance.
        candidate: HistoryPoint | None = None
        for point in reversed(buf):
            if point.ts_ms > target:
                continue
            if target - point.ts_ms <= tol:
                candidate = point
            break
        if candidate is None:
            return None
        quality = self.history_quality(symbol, interval_sec, now_ms)
        return WindowLookup(point=candidate, quality=quality)

    def turnover_delta(
        self,
        symbol: str,
        *,
        now_ms: int,
        interval_sec: int,
    ) -> float | None:
        latest = self.latest(symbol)
        past = self.lookup(symbol, now_ms=now_ms, interval_sec=interval_sec)
        if latest is None or past is None:
            return None
        if latest.source != HistorySource.TICKER or past.point.source != HistorySource.TICKER:
            return None
        delta = latest.turnover_24h - past.point.turnover_24h
        if delta < 0:
            return None
        return delta

    def baseline_turnover_delta(
        self,
        symbol: str,
        *,
        now_ms: int,
        interval_sec: int,
        windows: int,
    ) -> float | None:
        deltas: list[float] = []
        for i in range(1, windows + 1):
            end = now_ms - i * interval_sec * 1000
            start_lookup = self.lookup(symbol, now_ms=end, interval_sec=interval_sec)
            end_point = None
            buf = self._data.get(symbol)
            if not buf or start_lookup is None:
                continue
            # Find point nearest to end within tolerance.
            tol = window_tolerance_ms(interval_sec)
            for point in reversed(buf):
                if abs(point.ts_ms - end) <= tol and point.ts_ms <= end + tol:
                    end_point = point
                    break
            if end_point is None:
                continue
            delta = end_point.turnover_24h - start_lookup.point.turnover_24h
            if delta >= 0:
                deltas.append(delta)
        if not deltas:
            return None
        return sum(deltas) / len(deltas)

    def point_count(self, symbol: str) -> int:
        return len(self._data.get(symbol, ()))
