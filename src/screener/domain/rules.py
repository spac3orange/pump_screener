from __future__ import annotations

from screener.domain.enums import Direction, SignalType

DIRECTION_OF = {
    SignalType.PUMP: Direction.UP,
    SignalType.DROP: Direction.DOWN,
    SignalType.DUMP: Direction.DOWN,
}

SEVERITY = {
    SignalType.PUMP: 1,
    SignalType.DROP: 1,
    SignalType.DUMP: 2,
}


def price_change_pct(current: float, previous: float) -> float:
    if previous <= 0:
        raise ValueError("previous price must be > 0")
    return ((current - previous) / previous) * 100.0


def is_pump(change_pct: float, threshold: float) -> bool:
    return change_pct >= threshold


def is_dump(change_pct: float, threshold: float) -> bool:
    return change_pct <= -threshold


def window_tolerance_ms(interval_sec: int) -> int:
    """Allowed skew when looking up a historical price point."""
    return max(2_000, int(0.05 * interval_sec * 1000))


def window_tolerance_sec(interval_sec: int) -> int:
    return window_tolerance_ms(interval_sec) // 1000
