"""Binance ``!ticker@arr`` frames -> ordered ``PriceEvent`` list.

Runs synchronously in the stream reader. Every element of a frame is
processed in order; nothing is coalesced. Per symbol, the first ticker after a
(re)connect seeds history (``SNAPSHOT_SEED``), a later ticker with a new last
price ``c`` is ``LIVE``, and a ticker whose event time ``E`` is not newer
than the last one applied is out of order and ignored. ``q`` (24h quote
volume, USDT) is the 24h turnover.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from screener.application.metrics import FeedMetrics
from screener.application.price_history import PriceHistoryManager
from screener.domain.enums import PriceEventKind
from screener.domain.models import PriceEvent


@dataclass(slots=True)
class _State:
    last_price: float | None = None
    last_ts_ms: int = 0
    seeded: bool = False
    turnover_24h: float = 0.0
    volume_24h: float = 0.0


def _float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out


class BinanceTickerProcessor:
    def __init__(
        self, stats: FeedMetrics, history: PriceHistoryManager | None = None
    ) -> None:
        self._stats = stats
        self._history = history
        self._states: dict[str, _State] = {}
        self._universe: frozenset[str] = frozenset()

    def set_universe(self, symbols: Iterable[str]) -> None:
        self._universe = frozenset(symbols)

    def last_price(self, symbol: str) -> float | None:
        state = self._states.get(symbol)
        return state.last_price if state is not None else None

    def reset_symbol(self, symbol: str) -> None:
        self._states.pop(symbol, None)
        if self._history is not None:
            self._history.clear(symbol)

    def on_resubscribe(self, symbols: Iterable[str]) -> None:
        """New connection: the next ticker of every symbol is a seed, not a move."""
        for state in self._states.values():
            state.seeded = False

    def apply_message(
        self, message: dict[str, Any], *, mono: float | None = None
    ) -> list[PriceEvent] | None:
        data = message.get("data")
        if not isinstance(data, list):
            return None
        stats = self._stats
        universe = self._universe
        events: list[PriceEvent] = []
        received = time.perf_counter()
        for item in data:
            if not isinstance(item, dict):
                continue
            symbol = item.get("s")
            if symbol not in universe:
                continue
            ts_ms = item.get("E")
            price = _float(item.get("c"))
            if not isinstance(ts_ms, int) or price is None or price <= 0:
                continue
            stats.ticker_messages_total += 1
            state = self._states.get(symbol)
            if state is None:
                state = self._states[symbol] = _State()
            if state.seeded and ts_ms <= state.last_ts_ms:
                stats.out_of_order_total += 1
                continue
            turnover = _float(item.get("q"))
            volume = _float(item.get("v"))
            if turnover is not None:
                state.turnover_24h = turnover
            if volume is not None:
                state.volume_24h = volume
            if not state.seeded:
                state.seeded = True
                kind = PriceEventKind.SNAPSHOT_SEED
                stats.snapshot_seeds_total += 1
            elif price != state.last_price:
                kind = PriceEventKind.LIVE
                stats.live_price_events_total += 1
            else:
                state.last_ts_ms = ts_ms
                stats.enrichment_updates_total += 1
                continue
            state.last_ts_ms = ts_ms
            state.last_price = price
            events.append(
                PriceEvent(
                    symbol=symbol,
                    kind=kind,
                    last_price=price,
                    exchange_ts_ms=ts_ms,
                    received_mono=received,
                    turnover_24h=state.turnover_24h,
                    volume_24h=state.volume_24h,
                )
            )
        if events:
            stats.exchange_clock_skew_ms = time.time() * 1000 - events[-1].exchange_ts_ms
        return events
