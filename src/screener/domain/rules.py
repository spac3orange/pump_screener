from __future__ import annotations

from screener.domain.enums import SideHint, SignalType


def price_change_pct(current: float, previous: float) -> float:
    if previous <= 0:
        raise ValueError("previous price must be > 0")
    return ((current - previous) / previous) * 100.0


def is_pump(change_pct: float, threshold: float) -> bool:
    return change_pct >= threshold


def is_dump(change_pct: float, threshold: float) -> bool:
    return change_pct <= -threshold


def side_hint_for(signal_type: SignalType) -> SideHint:
    if signal_type == SignalType.PUMP:
        return SideHint.LONG
    if signal_type in {SignalType.SHORT, SignalType.DUMP}:
        return SideHint.SHORT
    return SideHint.FLAT


def window_tolerance_ms(interval_sec: int) -> int:
    """Allowed skew when looking up a historical price point."""
    return max(2_000, int(0.05 * interval_sec * 1000))
