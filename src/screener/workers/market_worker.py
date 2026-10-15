from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from screener.application.market_processor import normalize_klines
from screener.application.metrics import metrics
from screener.application.supervisor import cancel_tasks, supervise
from screener.config import MAX_RULE_INTERVAL_SEC
from screener.domain.enums import PriceEventKind
from screener.domain.models import Instrument, PriceEvent
from screener.logging import log

if TYPE_CHECKING:
    from screener.app_state import AppState
    from screener.application.commercial.multi_engine import MultiConfigEngine

logger = logging.getLogger(__name__)

_GAP_CHECK_DELAY_SEC = 5.0


class MarketWorker:
    """WS frames -> merged TickerState -> ordered PriceEvents -> history -> engine.

    Every live lastPrice change is processed in order; nothing is coalesced on
    the trigger path. When the price queue is full the WS reader waits, which
    backpressures the socket instead of dropping data.
    """

    def __init__(self, state: AppState) -> None:
        self._state = state
        self._queue: asyncio.Queue[PriceEvent] = asyncio.Queue(
            maxsize=state.system.market_queue_maxsize
        )
        self._tasks: list[asyncio.Task] = []
        self._background: set[asyncio.Task] = set()
        self._commercial_engine: MultiConfigEngine | None = getattr(
            state, "commercial_engine", None
        )

    @property
    def queue(self) -> asyncio.Queue[PriceEvent]:
        return self._queue

    async def on_ws_message(self, msg: dict, mono: float) -> None:
        event = self._state.processor.apply_message(msg, mono=mono)
        if event is None:
            return
        metrics.price_events_in += 1
        if self._queue.full():
            metrics.price_queue_blocked_total += 1
        await self._queue.put(event)
        metrics.price_queue_size = self._queue.qsize()

    def on_ws_connect(self, symbols: list[str]) -> None:
        log(
            logger,
            logging.INFO,
            f"WS shard connected, symbols={len(symbols)}",
            component="market_worker",
            event="ws_connect",
            count=len(symbols),
        )
        self._state.processor.on_resubscribe(symbols)
        if symbols and any(self._state.history.span(s) for s in symbols[:5]):
            self._spawn(self._backfill_gaps_later(symbols), "gap-backfill")

    def on_ws_disconnect(self, symbols: list[str], last_ts_ms: int | None) -> None:
        log(
            logger,
            logging.WARNING,
            f"WS shard disconnected, symbols={len(symbols)} last_ts_ms={last_ts_ms}",
            component="market_worker",
            event="ws_disconnect",
            count=len(symbols),
        )
        self._state.history.mark_disconnected(symbols, at_ts_ms=last_ts_ms)

    def process_event(self, event: PriceEvent) -> None:
        state = self._state
        try:
            state.history.record(
                event.symbol, ts_ms=event.exchange_ts_ms, price=event.last_price
            )
            if event.kind is PriceEventKind.LIVE:
                state.signal_engine.evaluate(event)
        except Exception:
            metrics.record_error()
            logger.exception("Price event processing failed for %s", event.symbol)
        # Commercial rules run after the admin engine and can never affect it.
        commercial = self._commercial_engine
        if commercial is not None and event.kind is PriceEventKind.LIVE:
            try:
                commercial.evaluate(event)
            except Exception:
                metrics.commercial_engine_errors_total += 1
                logger.exception("Commercial evaluation failed for %s", event.symbol)
        metrics.price_events_processed += 1
        metrics.price_queue_size = self._queue.qsize()
        lag = time.perf_counter() - event.received_mono
        metrics.record_latency(lag)
        if lag * 1000 > state.signal_engine.settings.max_tick_age_ms:
            metrics.late_price_events_total += 1

    async def start(self) -> None:
        state = self._state
        ws = state.ws_manager
        ws.on_message = self.on_ws_message
        ws.on_connect = self.on_ws_connect
        ws.on_disconnect = self.on_ws_disconnect
        state.registry._on_change = self._on_universe_change

        log(
            logger,
            logging.INFO,
            "Market worker starting",
            component="market_worker",
            event="start",
        )
        await state.registry.start()
        symbols = state.registry.symbols
        log(
            logger,
            logging.INFO,
            f"Registry ready with {len(symbols)} symbols; starting WS",
            component="market_worker",
            event="registry_ready",
            count=len(symbols),
        )
        self._tasks.append(supervise("market-process", self._process_loop))
        self._tasks.append(supervise("readiness", self._readiness_loop))
        self._tasks.append(supervise("loop-monitor", self._loop_monitor))
        await ws.start(symbols)
        self._spawn(self._warmup_symbols(symbols), "kline-warmup")

    async def stop(self) -> None:
        await self._state.ws_manager.stop()
        await self._state.registry.stop()
        await cancel_tasks(self._tasks)
        await cancel_tasks(list(self._background))
        self._state.correlator.flush_all()

    def _spawn(self, coro, name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _process_loop(self) -> None:
        queue = self._queue
        while True:
            event = await queue.get()
            self.process_event(event)
            queue.task_done()

    async def _on_universe_change(
        self, instruments: list[Instrument], added: set[str], removed: set[str]
    ) -> None:
        state = self._state
        symbols = [i.symbol for i in instruments]
        await state.ws_manager.update_symbols(symbols)
        for sym in removed:
            state.processor.reset_symbol(sym)
            state.signal_engine.dedup.clear_symbol(sym)
            state.notifications.forget_symbol(sym)
            if self._commercial_engine is not None:
                self._commercial_engine.clear_symbol(sym)
        if added and len(added) < len(symbols):
            self._spawn(self._warmup_symbols(sorted(added)), "kline-warmup-added")
        try:
            await state.repo.log_event(
                level="INFO",
                component="registry",
                event_type="universe_changed",
                message=f"added={len(added)} removed={len(removed)} total={len(symbols)}",
                details={"added": sorted(added)[:50], "removed": sorted(removed)[:50]},
            )
        except Exception:
            logger.warning("Failed to log universe change", exc_info=True)

    async def _readiness_loop(self) -> None:
        state = self._state
        was_ready: bool | None = None
        while True:
            age = state.ws_manager.last_message_age_sec
            fresh = age is not None and age < state.system.stale_connection_sec
            ready = bool(
                metrics.instruments_active > 0 and metrics.ws_connections > 0 and fresh
            )
            metrics.ready = ready
            metrics.last_message_age_ms = None if age is None else round(age * 1000, 1)
            if was_ready is None or ready != was_ready:
                log(
                    logger,
                    logging.INFO if ready else logging.WARNING,
                    f"Screener ready={ready} instruments={metrics.instruments_active} "
                    f"ws={metrics.ws_connections} last_msg_age_ms={metrics.last_message_age_ms}",
                    component="market_worker",
                    event="ready_changed",
                    status="ready" if ready else "not_ready",
                    count=metrics.instruments_active,
                )
                was_ready = ready
            await asyncio.sleep(1.0)

    async def _loop_monitor(self) -> None:
        interval = 0.5
        loop = asyncio.get_running_loop()
        ticks = 0
        while True:
            start = loop.time()
            await asyncio.sleep(interval)
            lag_ms = max(0.0, (loop.time() - start - interval) * 1000)
            metrics.event_loop_lag_ms = round(lag_ms, 2)
            metrics.event_loop_lag_max_ms = max(metrics.event_loop_lag_max_ms, lag_ms)
            ticks += 1
            if ticks % 2 == 0:
                metrics.update_rates()

    def _kline_limit(self) -> int:
        settings = self._state.signal_engine.settings
        longest = max(
            settings.long_interval_sec, settings.short_interval_sec, settings.dump_interval_sec
        )
        if self._commercial_engine is not None:
            # Subscribers may use any window up to 60 min; warm up the full hour.
            longest = max(longest, MAX_RULE_INTERVAL_SEC)
        return min(1000, max(self._state.system.kline_warmup_limit, longest // 60 + 4))

    async def _warmup_symbols(self, symbols: list[str]) -> None:
        if not symbols:
            return
        state = self._state
        sem = asyncio.Semaphore(state.system.kline_warmup_concurrency)
        limit = self._kline_limit()
        done = 0

        async def one(symbol: str) -> None:
            nonlocal done
            async with sem:
                try:
                    rows = await state.rest.fetch_kline(symbol, interval="1", limit=limit)
                except Exception:
                    logger.warning("Kline warmup failed for %s", symbol)
                    return
            closes = normalize_klines(rows, now_ms=int(time.time() * 1000))
            state.history.backfill_klines(symbol, closes)
            done += 1

        started = time.monotonic()
        await asyncio.gather(*(one(s) for s in symbols))
        logger.info(
            "Kline warmup: %s/%s symbols in %.1fs",
            done,
            len(symbols),
            time.monotonic() - started,
        )

    async def _backfill_gaps_later(self, symbols: list[str]) -> None:
        await asyncio.sleep(_GAP_CHECK_DELAY_SEC)
        state = self._state
        min_len = state.system.gap_backfill_min_sec
        sem = asyncio.Semaphore(state.system.kline_warmup_concurrency)

        async def one(symbol: str, gs: int, ge: int) -> None:
            async with sem:
                try:
                    rows = await state.rest.fetch_kline(
                        symbol,
                        interval="1",
                        limit=min(1000, (ge - gs) // 60 + 4),
                        start_ms=(gs - 120) * 1000,
                        end_ms=ge * 1000,
                    )
                except Exception:
                    logger.warning("Gap backfill failed for %s", symbol)
                    return
            closes = normalize_klines(rows, now_ms=int(time.time() * 1000))
            state.history.backfill_klines(symbol, closes, lo_sec=gs, hi_sec=ge)

        jobs = [
            one(symbol, gs, ge)
            for symbol in symbols
            for gs, ge in state.history.gaps(symbol, min_len_sec=min_len)
        ]
        if jobs:
            logger.info("Backfilling %s history gaps from REST klines", len(jobs))
            await asyncio.gather(*jobs)
