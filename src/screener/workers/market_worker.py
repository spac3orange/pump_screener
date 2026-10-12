from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from screener.application.metrics import metrics
from screener.domain.models import Instrument, SignalEvent

if TYPE_CHECKING:
    from screener.app_state import AppState

logger = logging.getLogger(__name__)


class MarketWorker:
    def __init__(self, state: AppState) -> None:
        self._state = state
        self._queue: asyncio.Queue[dict] = asyncio.Queue(
            maxsize=state.system.market_queue_maxsize
        )
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()

    async def start(self) -> None:
        self._stop.clear()
        state = self._state

        async def on_ws_message(msg: dict) -> None:
            try:
                self._queue.put_nowait(msg)
            except asyncio.QueueFull:
                try:
                    _ = self._queue.get_nowait()
                    self._queue.task_done()
                except asyncio.QueueEmpty:
                    pass
                metrics.record_drop()
                try:
                    self._queue.put_nowait(msg)
                except asyncio.QueueFull:
                    metrics.record_drop()
            metrics.queue_size = self._queue.qsize()

        # Wire WS callback
        state.ws_manager._on_message = on_ws_message

        async def on_universe_change(
            instruments: list[Instrument], added: set[str], removed: set[str]
        ) -> None:
            symbols = [i.symbol for i in instruments]
            await state.ws_manager.update_symbols(symbols)
            for sym in removed:
                state.processor.reset_symbol(sym)
            if added:
                await self._warmup_symbols(sorted(added))
            await state.repo.log_event(
                level="INFO",
                component="registry",
                event_type="universe_changed",
                message=f"added={len(added)} removed={len(removed)} total={len(symbols)}",
                details={"added": sorted(added)[:50], "removed": sorted(removed)[:50]},
            )

        state.registry._on_change = on_universe_change

        async def on_signal(signal: SignalEvent) -> None:
            try:
                await state.repo.save_signal(signal)
            except Exception:
                metrics.record_error()
                logger.exception("Failed to persist signal")
            await state.notifications.enqueue(signal)

        state.signal_engine._on_signal = on_signal

        await state.registry.start()
        symbols = state.registry.symbols
        # Start WS immediately; warmup continues in background so HTTP comes up fast.
        await state.ws_manager.start(symbols)
        self._tasks.append(asyncio.create_task(self._process_loop(), name="market-process"))
        self._tasks.append(asyncio.create_task(self._readiness_loop(), name="readiness"))
        self._tasks.append(
            asyncio.create_task(self._warmup_symbols(symbols), name="warmup")
        )

    async def stop(self) -> None:
        self._stop.set()
        await self._state.ws_manager.stop()
        await self._state.registry.stop()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _process_loop(self) -> None:
        while not self._stop.is_set():
            try:
                msg = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except TimeoutError:
                continue
            try:
                tick = await self._state.processor.handle_ws_message(msg)
                if tick is not None:
                    await self._state.signal_engine.on_tick(tick)
            except Exception:
                metrics.record_error()
                logger.exception("Market message processing failed")
            finally:
                self._queue.task_done()
                metrics.queue_size = self._queue.qsize()

    async def _readiness_loop(self) -> None:
        while not self._stop.is_set():
            age = self._state.ws_manager.last_message_age_sec
            instruments_ok = metrics.instruments_active > 0
            ws_ok = metrics.ws_connections > 0
            fresh = age is not None and age < self._state.system.stale_connection_sec
            metrics.ready = bool(instruments_ok and ws_ok and fresh)
            if age is not None:
                metrics.last_message_age_ms = age * 1000
                metrics.last_ws_message_ts = time.time() - age
            await asyncio.sleep(1.0)

    async def _warmup_symbols(self, symbols: list[str]) -> None:
        if not symbols:
            return
        state = self._state
        try:
            tickers = await state.rest.fetch_tickers(category="linear")
            wanted = set(symbols)
            now_ms = int(time.time() * 1000)
            for raw in tickers:
                if raw.get("symbol") in wanted:
                    state.processor.seed_from_rest_ticker(raw, now_ms=now_ms)
        except Exception:
            metrics.record_error()
            logger.exception("Ticker seed failed")

        sem = asyncio.Semaphore(state.system.kline_warmup_concurrency)

        async def one(symbol: str) -> None:
            async with sem:
                try:
                    rows = await state.rest.fetch_kline(
                        symbol,
                        interval="1",
                        limit=state.system.kline_warmup_limit,
                    )
                    state.processor.seed_from_kline_rows(symbol, rows)
                except Exception:
                    metrics.record_error()
                    logger.warning("Kline warmup failed for %s", symbol)

        # Warm a bounded batch to avoid HTTP IP pressure on huge universes.
        batch = symbols[:200]
        await asyncio.gather(*(one(s) for s in batch))
        logger.info("Warmup completed for %s symbols (batch of %s)", len(symbols), len(batch))
