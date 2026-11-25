from __future__ import annotations

from array import array
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from screener.application.metrics import metrics
from screener.domain.enums import HistoryQuality, HistorySource
from screener.domain.rules import window_tolerance_sec

_EMPTY = 0
_KLINE = 1
_TICKER = 2
_SRC_CODE = {HistorySource.KLINE: _KLINE, HistorySource.TICKER: _TICKER}

def history_capacity_for(max_interval_sec: int) -> int:
    """Ring length needed to look up a window of ``max_interval_sec``."""
    return max_interval_sec + window_tolerance_sec(max_interval_sec) + 60


# Boot capacity matches the old 60m rule ceiling; grows via ensure_capacity.
DEFAULT_CAPACITY_SEC = history_capacity_for(3_600)


@dataclass(slots=True, frozen=True)
class WindowLookup:
    price: float
    ts_sec: int
    quality: HistoryQuality


class _Ring:
    """Per-second lastPrice ring for one symbol, keyed by exchange-time seconds."""

    __slots__ = ("secs", "prices", "src", "first_sec", "last_sec", "broken", "gaps")

    def __init__(self, capacity: int) -> None:
        self.secs = array("q", [-1]) * capacity
        self.prices = array("d", [0.0]) * capacity
        self.src = bytearray(capacity)
        self.first_sec: int | None = None
        self.last_sec: int | None = None
        # Set on disconnect; the next write records a gap instead of carrying forward.
        self.broken = False
        self.gaps: list[list[int]] = []

    def reset(self) -> None:
        self.secs = array("q", [-1]) * len(self.secs)
        self.src = bytearray(len(self.src))
        self.first_sec = None
        self.last_sec = None
        self.broken = False
        self.gaps = []


class PriceHistoryManager:
    """In-memory 1 Hz lastPrice history with O(1) window lookup.

    Seconds come from Bybit message ``ts`` (market time), never local wall-clock.
    Between consecutive live writes on an uninterrupted connection the previous
    lastPrice is carried forward (it stayed the last traded price). Disconnects
    leave explicit gaps; a window is invalid only if its reference point is
    missing or a gap longer than the window tolerance falls inside it.
    """

    def __init__(self, *, capacity_sec: int = DEFAULT_CAPACITY_SEC) -> None:
        self._cap = capacity_sec
        self._rings: dict[str, _Ring] = {}
        self._needs_warmup: set[str] = set()

    @property
    def capacity_sec(self) -> int:
        return self._cap

    def ensure_capacity(self, capacity_sec: int) -> None:
        """Grow the ring if rules need a longer window; drops in-memory history."""
        if capacity_sec <= self._cap:
            return
        symbols = list(self._rings)
        self._cap = capacity_sec
        self._rings.clear()
        self._needs_warmup.update(symbols)

    def take_needs_warmup(self) -> list[str]:
        """Symbols whose history was dropped after an outage longer than the capacity."""
        symbols = sorted(self._needs_warmup)
        self._needs_warmup.clear()
        return symbols

    def clear(self, symbol: str | None = None) -> None:
        if symbol is None:
            self._rings.clear()
            self._needs_warmup.clear()
        else:
            self._rings.pop(symbol, None)
            self._needs_warmup.discard(symbol)

    def _ring(self, symbol: str) -> _Ring:
        ring = self._rings.get(symbol)
        if ring is None:
            ring = _Ring(self._cap)
            self._rings[symbol] = ring
        return ring

    def _fill(self, ring: _Ring, start: int, end: int, price: float, src: int) -> None:
        cap = self._cap
        secs, prices, codes = ring.secs, ring.prices, ring.src
        for s in range(start, end + 1):
            slot = s % cap
            secs[slot] = s
            prices[slot] = price
            codes[slot] = src

    def _trim(self, ring: _Ring) -> None:
        assert ring.last_sec is not None and ring.first_sec is not None
        min_valid = ring.last_sec - self._cap + 1
        if ring.first_sec < min_valid:
            ring.first_sec = min_valid
        if ring.gaps and ring.gaps[0][1] < min_valid:
            ring.gaps = [g for g in ring.gaps if g[1] >= min_valid]

    def record(
        self,
        symbol: str,
        *,
        ts_ms: int,
        price: float,
        source: HistorySource = HistorySource.TICKER,
    ) -> bool:
        """Store the latest lastPrice of second ``ts_ms // 1000``."""
        sec = ts_ms // 1000
        code = _SRC_CODE[source]
        ring = self._ring(symbol)
        last = ring.last_sec
        if last is None:
            self._fill(ring, sec, sec, price, code)
            ring.first_sec = ring.last_sec = sec
            ring.broken = False
            return True
        if sec < last:
            metrics.out_of_order_total += 1
            return False
        if sec == last:
            self._fill(ring, sec, sec, price, code)
            return True

        gap_len = sec - last - 1
        if gap_len >= self._cap:
            if ring.broken:
                self._needs_warmup.add(symbol)
            ring.reset()
            self._fill(ring, sec, sec, price, code)
            ring.first_sec = ring.last_sec = sec
            return True
        if ring.broken:
            if gap_len > 0:
                ring.gaps.append([last + 1, sec - 1])
                metrics.history_gaps_total += 1
            ring.broken = False
        elif gap_len > 0:
            slot = last % self._cap
            self._fill(ring, last + 1, sec - 1, ring.prices[slot], ring.src[slot])
        self._fill(ring, sec, sec, price, code)
        ring.last_sec = sec
        self._trim(ring)
        return True

    def mark_disconnected(self, symbols: Iterable[str], *, at_ts_ms: int | None) -> None:
        """Connection for ``symbols`` was lost after market time ``at_ts_ms``.

        History is first carried forward to the last frame seen on that
        connection, then the next write records a gap.
        """
        at_sec = None if at_ts_ms is None else at_ts_ms // 1000
        for symbol in symbols:
            ring = self._rings.get(symbol)
            if ring is None or ring.last_sec is None or ring.broken:
                continue
            last = ring.last_sec
            if at_sec is not None and last < at_sec < last + self._cap:
                slot = last % self._cap
                self._fill(ring, last + 1, at_sec, ring.prices[slot], ring.src[slot])
                ring.last_sec = at_sec
                self._trim(ring)
            ring.broken = True

    def lookup(self, symbol: str, *, now_ms: int, interval_sec: int) -> WindowLookup | None:
        ring = self._rings.get(symbol)
        if ring is None or ring.first_sec is None:
            return None
        metrics.history_lookups_total += 1
        now_sec = now_ms // 1000
        target = now_sec - interval_sec
        tol = window_tolerance_sec(interval_sec)
        lo = max(target - tol, ring.first_sec)
        cap = self._cap
        secs = ring.secs
        s = target
        while s >= lo:
            slot = s % cap
            if secs[slot] == s:
                break
            s -= 1
        else:
            return None
        for gs, ge in ring.gaps:
            if ge >= s and gs <= now_sec and ge - gs + 1 > tol:
                return None
        quality = HistoryQuality.MIXED if ring.src[slot] == _KLINE else HistoryQuality.LIVE
        return WindowLookup(price=ring.prices[slot], ts_sec=s, quality=quality)

    def backfill_klines(
        self,
        symbol: str,
        closes: Sequence[tuple[int, float]],
        *,
        lo_sec: int | None = None,
        hi_sec: int | None = None,
    ) -> int:
        """Fill empty seconds from 1m kline closes (ascending ``(close_sec, price)``).

        Never overwrites ticker data and never touches WS ticker state. Without
        bounds this is a startup warmup: it fills everything before the first
        live second. Returns the number of seconds written.
        """
        if not closes:
            return 0
        ring = self._ring(symbol)
        empty = ring.last_sec is None
        if hi_sec is None:
            hi_sec = closes[-1][0] if empty else ring.first_sec - 1  # type: ignore[operator]
        if lo_sec is None:
            lo_sec = closes[0][0]
        ref_last = hi_sec if empty else max(ring.last_sec, hi_sec)  # type: ignore[type-var]
        min_writable = ref_last - self._cap + 1
        cap = self._cap
        secs, prices, codes = ring.secs, ring.prices, ring.src
        written_from: int | None = None
        written = 0
        for i, (close_sec, price) in enumerate(closes):
            start = max(close_sec, lo_sec, min_writable)
            end = closes[i + 1][0] - 1 if i + 1 < len(closes) else hi_sec
            end = min(end, hi_sec)
            for s in range(start, end + 1):
                slot = s % cap
                if secs[slot] == s:
                    continue
                secs[slot] = s
                prices[slot] = price
                codes[slot] = _KLINE
                written += 1
                if written_from is None:
                    written_from = s
        if written_from is None:
            return 0
        covered_from = max(closes[0][0], lo_sec, min_writable)
        if empty:
            ring.first_sec = covered_from
            ring.last_sec = hi_sec
        elif covered_from < ring.first_sec:  # type: ignore[operator]
            ring.first_sec = covered_from
        kept: list[list[int]] = []
        for gs, ge in ring.gaps:
            if covered_from <= gs and hi_sec >= ge:
                continue
            if gs < covered_from <= ge and hi_sec >= ge:
                kept.append([gs, covered_from - 1])
            else:
                kept.append([gs, ge])
        ring.gaps = kept
        metrics.history_backfills_total += 1
        return written

    def gaps(self, symbol: str, *, min_len_sec: int = 1) -> list[tuple[int, int]]:
        ring = self._rings.get(symbol)
        if ring is None:
            return []
        return [(gs, ge) for gs, ge in ring.gaps if ge - gs + 1 >= min_len_sec]

    def span(self, symbol: str) -> tuple[int, int] | None:
        ring = self._rings.get(symbol)
        if ring is None or ring.first_sec is None or ring.last_sec is None:
            return None
        return ring.first_sec, ring.last_sec

    def latest_price(self, symbol: str) -> float | None:
        ring = self._rings.get(symbol)
        if ring is None or ring.last_sec is None:
            return None
        return ring.prices[ring.last_sec % self._cap]

    def point_count(self, symbol: str) -> int:
        ring = self._rings.get(symbol)
        if ring is None or ring.first_sec is None or ring.last_sec is None:
            return 0
        cap = self._cap
        return sum(
            1 for s in range(ring.first_sec, ring.last_sec + 1) if ring.secs[s % cap] == s
        )

    def symbols(self) -> list[str]:
        return list(self._rings)
