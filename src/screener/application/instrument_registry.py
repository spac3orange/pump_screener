from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence

from screener.application.metrics import metrics
from screener.domain.models import Instrument
from screener.infrastructure.bybit.mapping import is_crypto_usdt_linear_perpetual, map_instrument
from screener.infrastructure.bybit.rest import BybitRestClient

logger = logging.getLogger(__name__)

OnChange = Callable[[list[Instrument], set[str], set[str]], Awaitable[None]]


class InstrumentRegistry:
    def __init__(
        self,
        rest: BybitRestClient,
        *,
        refresh_sec: int = 300,
        exclude_symbols: set[str] | None = None,
        on_change: OnChange | None = None,
        instrument_sink: object | None = None,
    ) -> None:
        self._rest = rest
        self._refresh_sec = refresh_sec
        self._exclude_symbols = {s.upper() for s in (exclude_symbols or set())}
        self._on_change = on_change
        self._instrument_sink = instrument_sink
        self._instruments: dict[str, Instrument] = {}
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    @property
    def symbols(self) -> list[str]:
        return sorted(self._instruments)

    @property
    def instruments(self) -> list[Instrument]:
        return list(self._instruments.values())

    def set_exclude_symbols(self, symbols: set[str]) -> None:
        self._exclude_symbols = {s.upper() for s in symbols}

    async def start(self) -> None:
        await self.refresh()
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="instrument-registry")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._refresh_sec)
                break
            except TimeoutError:
                await self.refresh()

    async def refresh(self) -> None:
        try:
            raw_list = await self._rest.fetch_linear_instruments()
        except Exception:
            metrics.record_error()
            logger.exception("Instrument refresh failed; keeping previous universe")
            return

        filtered = [
            map_instrument(raw)
            for raw in raw_list
            if is_crypto_usdt_linear_perpetual(raw)
            and str(raw.get("symbol", "")).upper() not in self._exclude_symbols
        ]
        new_map = {i.symbol: i for i in filtered}
        old_symbols = set(self._instruments)
        new_symbols = set(new_map)
        added = new_symbols - old_symbols
        removed = old_symbols - new_symbols
        self._instruments = new_map
        metrics.instruments_active = len(self._instruments)

        if self._instrument_sink is not None and hasattr(self._instrument_sink, "upsert_instruments"):
            try:
                await self._instrument_sink.upsert_instruments(filtered)  # type: ignore[attr-defined]
            except Exception:
                logger.exception("Failed to persist instruments snapshot")

        if added or removed or not old_symbols:
            logger.info(
                "Universe updated: total=%s added=%s removed=%s",
                len(new_symbols),
                len(added),
                len(removed),
            )
            if self._on_change:
                await self._on_change(filtered, added, removed)

    def filter_symbols(self, symbols: Sequence[str]) -> list[str]:
        active = set(self._instruments)
        return [s for s in symbols if s in active]
