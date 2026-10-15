from __future__ import annotations

from dataclasses import dataclass

from screener.domain.enums import DedupState, SignalType


@dataclass(slots=True)
class DedupKey:
    symbol: str
    signal_type: SignalType
    interval_sec: int

    def as_tuple(self) -> tuple[str, str, int]:
        return (self.symbol, self.signal_type.value, self.interval_sec)


@dataclass(slots=True)
class ActiveState:
    level: int
    peak_pct: float
    last_notify_ms: int
    below_since_ms: int | None = None


@dataclass(slots=True, frozen=True)
class DedupDecision:
    should_notify: bool
    level: int
    reason: str


class SignalDeduplicator:
    """Deterministic cooldown / escalate / reset state machine."""

    def __init__(
        self,
        *,
        cooldown_sec: int = 300,
        escalate_step_pct: float = 2.0,
        reset_ratio: float = 0.5,
        reset_hold_sec: int = 60,
    ) -> None:
        self.cooldown_sec = cooldown_sec
        self.escalate_step_pct = escalate_step_pct
        self.reset_ratio = reset_ratio
        self.reset_hold_sec = reset_hold_sec
        self._state: dict[tuple[str, str, int], ActiveState] = {}

    def configure(
        self,
        *,
        cooldown_sec: int | None = None,
        escalate_step_pct: float | None = None,
        reset_ratio: float | None = None,
        reset_hold_sec: int | None = None,
    ) -> None:
        if cooldown_sec is not None:
            self.cooldown_sec = cooldown_sec
        if escalate_step_pct is not None:
            self.escalate_step_pct = escalate_step_pct
        if reset_ratio is not None:
            self.reset_ratio = reset_ratio
        if reset_hold_sec is not None:
            self.reset_hold_sec = reset_hold_sec

    def evaluate(
        self,
        key: DedupKey,
        *,
        change_pct: float,
        threshold_pct: float,
        now_ms: int,
        triggered: bool,
    ) -> DedupDecision:
        tkey = key.as_tuple()
        abs_change = abs(change_pct)
        reset_threshold = threshold_pct * self.reset_ratio
        current = self._state.get(tkey)

        if not triggered:
            if current is None:
                return DedupDecision(False, 0, "idle")
            # Reset when magnitude falls below reset threshold
            # (or sign flip handled by caller via triggered=False).
            if abs_change < reset_threshold:
                if current.below_since_ms is None:
                    current.below_since_ms = now_ms
                elif now_ms - current.below_since_ms >= self.reset_hold_sec * 1000:
                    del self._state[tkey]
                    return DedupDecision(False, 0, "reset")
            else:
                current.below_since_ms = None
            return DedupDecision(False, current.level, "active_no_trigger")

        # Triggered
        if current is None:
            self._state[tkey] = ActiveState(
                level=1,
                peak_pct=abs_change,
                last_notify_ms=now_ms,
            )
            return DedupDecision(True, 1, "first")

        current.below_since_ms = None
        cooldown_ok = now_ms - current.last_notify_ms >= self.cooldown_sec * 1000
        escalate_ok = abs_change >= current.peak_pct + self.escalate_step_pct
        if cooldown_ok and escalate_ok:
            current.level += 1
            current.peak_pct = abs_change
            current.last_notify_ms = now_ms
            return DedupDecision(True, current.level, "escalate")

        # Track peak even without notify
        if abs_change > current.peak_pct:
            current.peak_pct = abs_change
        return DedupDecision(False, current.level, "suppressed")

    def state_of(self, key: DedupKey) -> DedupState:
        return DedupState.ACTIVE if key.as_tuple() in self._state else DedupState.IDLE

    def has_state(self, symbol: str, signal_type: SignalType, interval_sec: int) -> bool:
        return (symbol, signal_type.value, interval_sec) in self._state

    @property
    def active_count(self) -> int:
        return len(self._state)

    def clear(self) -> None:
        self._state.clear()

    def clear_symbol(self, symbol: str) -> int:
        stale = [k for k in self._state if k[0] == symbol]
        for k in stale:
            del self._state[k]
        return len(stale)
