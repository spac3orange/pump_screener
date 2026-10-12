from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from screener.application.clock import SystemClock
from screener.application.dedup import DedupKey, SignalDeduplicator
from screener.application.metrics import metrics
from screener.application.ports import Clock
from screener.application.price_history import PriceHistoryManager
from screener.config import ScreenerSettings
from screener.domain.enums import HistoryQuality, SignalType
from screener.domain.models import ConditionResult, MarketTick, SignalEvent
from screener.domain.rules import is_dump, is_pump, price_change_pct, side_hint_for

logger = logging.getLogger(__name__)

SignalHandler = Callable[[SignalEvent], Awaitable[None] | None]


class SignalEngine:
    def __init__(
        self,
        history: PriceHistoryManager,
        settings: ScreenerSettings,
        *,
        clock: Clock | None = None,
        dedup: SignalDeduplicator | None = None,
        on_signal: SignalHandler | None = None,
    ) -> None:
        self._history = history
        self._settings = settings
        self._clock = clock or SystemClock()
        self._dedup = dedup or SignalDeduplicator(
            cooldown_sec=settings.cooldown_sec,
            escalate_step_pct=settings.escalate_step_pct,
            reset_ratio=settings.reset_ratio,
            reset_hold_sec=settings.reset_hold_sec,
        )
        self._on_signal = on_signal

    @property
    def settings(self) -> ScreenerSettings:
        return self._settings

    def update_settings(self, settings: ScreenerSettings) -> None:
        self._settings = settings
        self._dedup.configure(
            cooldown_sec=settings.cooldown_sec,
            escalate_step_pct=settings.escalate_step_pct,
            reset_ratio=settings.reset_ratio,
            reset_hold_sec=settings.reset_hold_sec,
        )

    async def on_tick(self, tick: MarketTick) -> list[SignalEvent]:
        settings = self._settings
        if tick.symbol.upper() in settings.exclude_symbols:
            return []

        now_ms = self._clock.time_ms()
        age = now_ms - tick.received_ts_ms
        if age > settings.max_tick_age_ms:
            return []

        emitted: list[SignalEvent] = []
        for interval in settings.signal_intervals_sec:
            emitted.extend(await self._evaluate_interval(tick, interval, now_ms))
        return emitted

    async def _evaluate_interval(
        self, tick: MarketTick, interval_sec: int, now_ms: int
    ) -> list[SignalEvent]:
        settings = self._settings
        lookup = self._history.lookup(tick.symbol, now_ms=now_ms, interval_sec=interval_sec)
        if lookup is None:
            return []

        quality = lookup.quality
        if settings.warmup_suppress_until_live and quality != HistoryQuality.LIVE:
            return []

        try:
            change = price_change_pct(tick.last_price, lookup.point.last_price)
        except ValueError:
            return []

        vol_ratio, turnover_delta = self._volume_metrics(tick.symbol, interval_sec, now_ms)
        min_turnover_ok = (tick.turnover_24h or 0) >= settings.min_turnover_24h_usdt

        results: list[SignalEvent] = []

        if settings.enable_pump:
            triggered = is_pump(change, settings.pump_threshold_pct) and min_turnover_ok
            event = await self._maybe_emit(
                tick,
                signal_type=SignalType.PUMP,
                interval_sec=interval_sec,
                change=change,
                threshold=settings.pump_threshold_pct,
                triggered=triggered,
                prev_price=lookup.point.last_price,
                quality=quality,
                vol_ratio=vol_ratio,
                now_ms=now_ms,
                conditions=[
                    ConditionResult("price_change", triggered, {"change_pct": change}),
                    ConditionResult("min_turnover_24h", min_turnover_ok, {
                        "turnover_24h": tick.turnover_24h,
                        "min": settings.min_turnover_24h_usdt,
                    }),
                ],
            )
            if event:
                results.append(event)

        if settings.enable_dump:
            triggered = is_dump(change, settings.dump_threshold_pct) and min_turnover_ok
            event = await self._maybe_emit(
                tick,
                signal_type=SignalType.DUMP,
                interval_sec=interval_sec,
                change=change,
                threshold=settings.dump_threshold_pct,
                triggered=triggered,
                prev_price=lookup.point.last_price,
                quality=quality,
                vol_ratio=vol_ratio,
                now_ms=now_ms,
                conditions=[
                    ConditionResult("price_change", triggered, {"change_pct": change}),
                    ConditionResult("min_turnover_24h", min_turnover_ok, {
                        "turnover_24h": tick.turnover_24h,
                        "min": settings.min_turnover_24h_usdt,
                    }),
                ],
            )
            if event:
                results.append(event)

        if settings.enable_volume_spike:
            spike_ok = (
                vol_ratio is not None
                and turnover_delta is not None
                and vol_ratio >= settings.volume_ratio_threshold
                and turnover_delta >= settings.min_abs_turnover_window_usdt
                and min_turnover_ok
            )
            event = await self._maybe_emit(
                tick,
                signal_type=SignalType.VOLUME_SPIKE,
                interval_sec=interval_sec,
                change=change,
                threshold=settings.volume_ratio_threshold,
                triggered=bool(spike_ok),
                prev_price=lookup.point.last_price,
                quality=quality,
                vol_ratio=vol_ratio,
                now_ms=now_ms,
                conditions=[
                    ConditionResult(
                        "volume_spike",
                        bool(spike_ok),
                        {
                            "volume_ratio": vol_ratio,
                            "turnover_delta": turnover_delta,
                            "threshold": settings.volume_ratio_threshold,
                        },
                    ),
                    ConditionResult("min_turnover_24h", min_turnover_ok, {
                        "turnover_24h": tick.turnover_24h,
                    }),
                ],
            )
            if event:
                results.append(event)

        # Composite: pump/dump + volume spike together
        if settings.enable_pump and settings.enable_volume_spike:
            pump_ok = is_pump(change, settings.pump_threshold_pct)
            spike_ok = (
                vol_ratio is not None
                and turnover_delta is not None
                and vol_ratio >= settings.volume_ratio_threshold
                and turnover_delta >= settings.min_abs_turnover_window_usdt
            )
            triggered = pump_ok and spike_ok and min_turnover_ok
            event = await self._maybe_emit(
                tick,
                signal_type=SignalType.COMPOSITE,
                interval_sec=interval_sec,
                change=change,
                threshold=settings.pump_threshold_pct,
                triggered=triggered,
                prev_price=lookup.point.last_price,
                quality=quality,
                vol_ratio=vol_ratio,
                now_ms=now_ms,
                conditions=[
                    ConditionResult("pump", pump_ok, {"change_pct": change}),
                    ConditionResult("volume_spike", bool(spike_ok), {"volume_ratio": vol_ratio}),
                    ConditionResult("min_turnover_24h", min_turnover_ok, {
                        "turnover_24h": tick.turnover_24h,
                    }),
                ],
            )
            if event:
                results.append(event)

        return results

    def _volume_metrics(
        self, symbol: str, interval_sec: int, now_ms: int
    ) -> tuple[float | None, float | None]:
        delta = self._history.turnover_delta(
            symbol, now_ms=now_ms, interval_sec=interval_sec
        )
        if delta is None:
            return None, None
        baseline = self._history.baseline_turnover_delta(
            symbol,
            now_ms=now_ms,
            interval_sec=interval_sec,
            windows=self._settings.volume_baseline_windows,
        )
        if baseline is None or baseline <= 0:
            return None, delta
        return delta / baseline, delta

    async def _maybe_emit(
        self,
        tick: MarketTick,
        *,
        signal_type: SignalType,
        interval_sec: int,
        change: float,
        threshold: float,
        triggered: bool,
        prev_price: float,
        quality: HistoryQuality,
        vol_ratio: float | None,
        now_ms: int,
        conditions: list[ConditionResult],
    ) -> SignalEvent | None:
        decision = self._dedup.evaluate(
            DedupKey(tick.symbol, signal_type, interval_sec),
            change_pct=change if signal_type != SignalType.VOLUME_SPIKE else (vol_ratio or 0.0),
            threshold_pct=threshold,
            now_ms=now_ms,
            triggered=triggered,
        )
        if not decision.should_notify:
            return None

        event = SignalEvent(
            symbol=tick.symbol,
            signal_type=signal_type,
            side_hint=side_hint_for(signal_type),
            interval_sec=interval_sec,
            change_pct=change,
            price=tick.last_price,
            prev_price=prev_price,
            detected_at=datetime.fromtimestamp(now_ms / 1000, tz=UTC),
            rule_version=self._settings.rule_version(),
            turnover_24h=tick.turnover_24h,
            volume_24h=tick.volume_24h,
            volume_ratio=vol_ratio,
            history_quality=quality,
            level=decision.level,
            conditions=conditions,
        )
        metrics.record_signal()
        if self._on_signal is not None:
            result = self._on_signal(event)
            if hasattr(result, "__await__"):
                await result  # type: ignore[misc]
        return event
