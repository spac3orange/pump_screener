from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from typing import Any

from screener.application.metrics import metrics
from screener.application.price_history import PriceHistoryManager
from screener.domain.enums import PriceEventKind
from screener.domain.models import PriceEvent, TickerState

logger = logging.getLogger(__name__)


def normalize_klines(rows: list[list[str]], *, now_ms: int) -> list[tuple[int, float]]:
    """Bybit 1m klines (newest first) -> ascending ``(close_sec, close)``.

    The still-open candle is dropped: its close is not a settled price.
    """
    out: list[tuple[int, float]] = []
    for row in rows:
        if len(row) < 5:
            continue
        try:
            start_ms = int(row[0])
            close = float(row[4])
        except (TypeError, ValueError):
            continue
        if close <= 0 or start_ms + 60_000 > now_ms:
            continue
        out.append((start_ms // 1000 + 59, close))
    out.sort(key=lambda item: item[0])
    return out


class MarketDataProcessor:
    """Merges Bybit ticker snapshot/delta into canonical TickerState.

    Runs synchronously inside the WS shard reader, before any queue, so no raw
    protocol message can be dropped or reordered downstream. Emits a PriceEvent
    only when lastPrice is set by a snapshot (seed) or changed by a delta (live);
    every other field is merged in place (non-trigger enrichment).
    """

    def __init__(self, history: PriceHistoryManager | None = None) -> None:
        self._history = history
        self._states: dict[str, TickerState] = {}

    def get_state(self, symbol: str) -> TickerState | None:
        return self._states.get(symbol)

    def reset_symbol(self, symbol: str) -> None:
        self._states.pop(symbol, None)
        if self._history is not None:
            self._history.clear(symbol)

    def on_resubscribe(self, symbols: Iterable[str]) -> None:
        """Connection (re)established: wait for a fresh snapshot per symbol."""
        for symbol in symbols:
            state = self._states.get(symbol)
            if state is not None:
                state.reset_snapshot()

    def apply_message(
        self, message: dict[str, Any], *, mono: float | None = None
    ) -> PriceEvent | None:
        topic = message.get("topic")
        if not isinstance(topic, str) or not topic.startswith("tickers."):
            return None
        metrics.ticker_messages_total += 1
        symbol = topic[8:]
        msg_type = message.get("type") or "snapshot"
        data = message.get("data")
        if isinstance(data, list):
            data = data[0] if data else None
        if not isinstance(data, dict):
            return None
        ts_raw = message.get("ts")
        if ts_raw is None:
            return None
        ts_ms = int(ts_raw)
        cs_raw = message.get("cs")
        cs = int(cs_raw) if cs_raw is not None else None
        if mono is None:
            mono = time.monotonic()

        state = self._states.get(symbol)
        if state is None:
            state = TickerState(symbol=symbol)
            self._states[symbol] = state

        changed = state.apply(data, msg_type=msg_type, ts_ms=ts_ms, cs=cs, mono=mono)
        if changed is None:
            if msg_type == "delta" and not state.has_snapshot:
                metrics.deltas_before_snapshot_total += 1
            else:
                metrics.out_of_order_total += 1
            return None
        metrics.exchange_clock_skew_ms = time.time() * 1000 - ts_ms
        if state.last_price is None:
            return None
        if msg_type == "snapshot":
            metrics.snapshot_seeds_total += 1
            kind = PriceEventKind.SNAPSHOT_SEED
        elif changed:
            metrics.live_price_events_total += 1
            kind = PriceEventKind.LIVE
        else:
            metrics.enrichment_updates_total += 1
            return None
        return PriceEvent(
            symbol=symbol,
            kind=kind,
            last_price=state.last_price,
            exchange_ts_ms=ts_ms,
            received_mono=time.perf_counter(),
            turnover_24h=state.turnover_24h or 0.0,
            volume_24h=state.volume_24h or 0.0,
        )
