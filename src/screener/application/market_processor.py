from __future__ import annotations

import logging
import time
from typing import Any

from screener.application.clock import SystemClock
from screener.application.metrics import metrics
from screener.application.ports import Clock
from screener.application.price_history import PriceHistoryManager
from screener.domain.enums import HistorySource
from screener.domain.models import MarketTick, TickerState

logger = logging.getLogger(__name__)


class MarketDataProcessor:
    def __init__(
        self,
        history: PriceHistoryManager,
        *,
        max_tick_age_ms: int = 5000,
        clock: Clock | None = None,
        on_tick: object | None = None,
    ) -> None:
        self._history = history
        self._max_tick_age_ms = max_tick_age_ms
        self._clock = clock or SystemClock()
        self._on_tick = on_tick
        self._states: dict[str, TickerState] = {}

    def get_state(self, symbol: str) -> TickerState | None:
        return self._states.get(symbol)

    def reset_symbol(self, symbol: str) -> None:
        self._states.pop(symbol, None)
        self._history.clear(symbol)

    async def handle_ws_message(self, message: dict[str, Any]) -> MarketTick | None:
        topic = message.get("topic")
        if not isinstance(topic, str) or not topic.startswith("tickers."):
            return None

        metrics.record_message(received_ts=time.time())
        symbol = topic.split(".", 1)[1]
        msg_type = str(message.get("type") or "snapshot")
        ts_ms = int(message.get("ts") or self._clock.time_ms())
        cs = message.get("cs")
        cs_i = int(cs) if cs is not None else None
        data = message.get("data")
        if isinstance(data, list):
            if not data:
                return None
            data = data[0]
        if not isinstance(data, dict):
            return None

        state = self._states.get(symbol)
        if state is None:
            state = TickerState(symbol=symbol)
            self._states[symbol] = state

        prev_cs = state.cs
        state.apply_fields(data, msg_type=msg_type, ts_ms=ts_ms, cs=cs_i)
        if not state.has_snapshot or state.last_price is None or state.last_price <= 0:
            return None
        if state.turnover_24h is None:
            state.turnover_24h = 0.0
        if state.volume_24h is None:
            state.volume_24h = 0.0

        # Duplicate / non-progressing update
        if prev_cs is not None and state.cs == prev_cs and msg_type == "delta":
            if "lastPrice" not in data and "turnover24h" not in data:
                return None

        now_ms = self._clock.time_ms()
        state.received_ts_ms = now_ms
        age = now_ms - ts_ms
        metrics.processing_lag_ms = max(0.0, float(age))
        if age > self._max_tick_age_ms:
            return None

        tick = MarketTick(
            symbol=symbol,
            last_price=state.last_price,
            turnover_24h=float(state.turnover_24h),
            volume_24h=float(state.volume_24h),
            exchange_ts_ms=ts_ms,
            received_ts_ms=now_ms,
            source=HistorySource.TICKER,
        )
        self._history.add_tick(
            symbol,
            ts_ms=now_ms,
            last_price=tick.last_price,
            turnover_24h=tick.turnover_24h,
            source=HistorySource.TICKER,
        )
        if self._on_tick is not None:
            result = self._on_tick(tick)
            if hasattr(result, "__await__"):
                await result  # type: ignore[misc]
        return tick

    def seed_from_rest_ticker(self, raw: dict[str, Any], *, now_ms: int | None = None) -> None:
        symbol = str(raw.get("symbol") or "")
        if not symbol:
            return
        try:
            last_price = float(raw["lastPrice"])
            turnover = float(raw.get("turnover24h") or 0)
            volume = float(raw.get("volume24h") or 0)
        except (KeyError, TypeError, ValueError):
            return
        if last_price <= 0:
            return
        ts = now_ms or self._clock.time_ms()
        state = TickerState(
            symbol=symbol,
            last_price=last_price,
            turnover_24h=turnover,
            volume_24h=volume,
            exchange_ts_ms=ts,
            received_ts_ms=ts,
            has_snapshot=True,
        )
        self._states[symbol] = state
        self._history.seed_point(
            symbol,
            ts_ms=ts,
            last_price=last_price,
            turnover_24h=turnover,
            source=HistorySource.TICKER,
        )

    def seed_from_kline_rows(
        self,
        symbol: str,
        rows: list[list[str]],
    ) -> None:
        """Warm price history from kline closes only.

        Candle turnover is NOT turnover24h — store 0 and keep source=kline so
        volume-spike logic ignores these points.
        """
        ordered = list(reversed(rows))
        for row in ordered:
            if len(row) < 5:
                continue
            try:
                ts_ms = int(row[0])
                close = float(row[4])
            except (TypeError, ValueError):
                continue
            if close <= 0:
                continue
            self._history.seed_point(
                symbol,
                ts_ms=ts_ms,
                last_price=close,
                turnover_24h=0.0,
                source=HistorySource.KLINE,
            )
