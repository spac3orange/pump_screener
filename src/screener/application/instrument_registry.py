from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import httpx

from screener.application.metrics import metrics
from screener.domain.models import Instrument
from screener.infrastructure.bybit.mapping import is_crypto_usdt_linear_perpetual, map_instrument
from screener.infrastructure.bybit.rest import BybitRestClient
from screener.logging import log

logger = logging.getLogger(__name__)

OnChange = Callable[[list[Instrument], set[str], set[str]], Awaitable[None]]
FetchInstruments = Callable[[], Awaitable[list[dict[str, Any]]]]


class InstrumentRegistry:
    """Tradable universe of one exchange (Bybit by default).

    Another exchange plugs in its own ``fetch``/``accept``/``mapper`` and a
    ``stats`` object for its counters.
    """

    def __init__(
        self,
        rest: BybitRestClient | None,
        *,
        refresh_sec: int = 300,
        exclude_symbols: set[str] | None = None,
        on_change: OnChange | None = None,
        instrument_sink: object | None = None,
        fetch: FetchInstruments | None = None,
        accept: Callable[[dict[str, Any]], bool] = is_crypto_usdt_linear_perpetual,
        mapper: Callable[[dict[str, Any]], Instrument] = map_instrument,
        stats: Any = None,
        exchange: str = "bybit",
    ) -> None:
        self._rest = rest
        if fetch is None:
            assert rest is not None, "rest or fetch is required"
            fetch = rest.fetch_linear_instruments
        self._fetch = fetch
        self._accept = accept
        self._mapper = mapper
        self._stats = stats if stats is not None else metrics
        self.exchange = exchange
        self._refresh_sec = refresh_sec
        self._exclude_symbols = {s.upper() for s in (exclude_symbols or set())}
        self._on_change = on_change
        self._instrument_sink = instrument_sink
        self._instruments: dict[str, Instrument] = {}
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    @property
    def _log_prefix(self) -> str:
        return "" if self.exchange == "bybit" else f"[{self.exchange}] "

    @property
    def symbols(self) -> list[str]:
        return sorted(self._instruments)

    @property
    def instruments(self) -> list[Instrument]:
        return list(self._instruments.values())

    def set_exclude_symbols(self, symbols: set[str]) -> None:
        self._exclude_symbols = {s.upper() for s in symbols}

    async def start(self) -> None:
        log(
            logger,
            logging.INFO,
            f"Instrument registry starting (refresh every {self._refresh_sec}s)",
            component="registry",
            event="start",
        )
        await self.refresh()
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name=f"instrument-registry-{self.exchange}")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        log(logger, logging.INFO, "Instrument registry stopped", component="registry", event="stop")

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._refresh_sec)
                break
            except TimeoutError:
                log(
                    logger,
                    logging.DEBUG,
                    "Periodic instrument refresh",
                    component="registry",
                    event="refresh_tick",
                )
                await self.refresh()

    async def refresh(self) -> None:
        started = time.perf_counter()
        previous = len(self._instruments)
        try:
            raw_list = await self._fetch()
        except httpx.TimeoutException as exc:
            metrics.record_error()
            log(
                logger,
                logging.WARNING,
                f"Instrument refresh timeout ({type(exc).__name__}); "
                f"keeping previous universe ({previous} symbols)",
                component="registry",
                event="refresh_timeout",
                count=previous,
            )
            return
        except httpx.HTTPError as exc:
            metrics.record_error()
            log(
                logger,
                logging.WARNING,
                f"Instrument refresh network error ({type(exc).__name__}: {exc}); "
                f"keeping previous universe ({previous} symbols)",
                component="registry",
                event="refresh_network_error",
                count=previous,
            )
            return
        except Exception:
            metrics.record_error()
            log(
                logger,
                logging.ERROR,
                f"{self._log_prefix}Instrument refresh failed; "
                f"keeping previous universe ({previous} symbols)",
                component="registry",
                event="refresh_failed",
                count=previous,
                exc_info=True,
            )
            return

        filtered = [
            self._mapper(raw)
            for raw in raw_list
            if self._accept(raw) and str(raw.get("symbol", "")).upper() not in self._exclude_symbols
        ]
        new_map = {i.symbol: i for i in filtered}
        old_symbols = set(self._instruments)
        new_symbols = set(new_map)
        added = new_symbols - old_symbols
        removed = old_symbols - new_symbols
        self._instruments = new_map
        self._stats.instruments_active = len(self._instruments)
        duration_ms = round((time.perf_counter() - started) * 1000, 1)

        sink = self._instrument_sink
        if sink is not None and hasattr(sink, "upsert_instruments"):
            try:
                await sink.upsert_instruments(filtered)  # type: ignore[attr-defined]
            except Exception:
                log(
                    logger,
                    logging.ERROR,
                    "Failed to persist instruments snapshot",
                    component="registry",
                    event="persist_failed",
                    exc_info=True,
                )

        if added or removed or not old_symbols:
            log(
                logger,
                logging.INFO,
                f"{self._log_prefix}Universe updated: total={len(new_symbols)} added={len(added)} "
                f"removed={len(removed)} duration_ms={duration_ms}",
                component="registry",
                event="universe_updated",
                count=len(new_symbols),
                duration_ms=duration_ms,
                extra={"added": sorted(added)[:20], "removed": sorted(removed)[:20]},
            )
            if self._on_change:
                await self._on_change(filtered, added, removed)
        else:
            log(
                logger,
                logging.DEBUG,
                f"Universe unchanged: total={len(new_symbols)} duration_ms={duration_ms}",
                component="registry",
                event="universe_unchanged",
                count=len(new_symbols),
                duration_ms=duration_ms,
            )

    def filter_symbols(self, symbols: Sequence[str]) -> list[str]:
        active = set(self._instruments)
        return [s for s in symbols if s in active]
