from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from screener.application.market_feed import MarketFeed
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
_KLINE_ATTEMPTS = 3
_KLINE_RETRY_SEC = 5.0


class MarketWorker:
    """WS frames -> processor -> ordered PriceEvents -> history -> engine.

    Every live lastPrice change is processed in order; nothing is coalesced on
    the trigger path. When the price queue is full the WS reader waits, which
    backpressures the socket instead of dropping data. Each exchange has its
    own worker and queue so one feed cannot stall another.
    """

    def __init__(self, state: AppState, *, feed: MarketFeed | None = None) -> None:
        self._state = state
        self._feed = feed or MarketFeed.from_app_state(state)
        self._queue: asyncio.Queue[PriceEvent] = asyncio.Queue(
            maxsize=state.system.market_queue_maxsize
        )
        self._tasks: list[asyncio.Task] = []
        self._background: set[asyncio.Task] = set()
        self._kline_sem: asyncio.Semaphore | None = None
        self._commercial_engine: MultiConfigEngine | None = self._feed.commercial_engine
        if self._commercial_engine is None:
            self._commercial_engine = getattr(state, "commercial_engine", None)

    @property
    def feed(self) -> MarketFeed:
        return self._feed

    @property
    def queue(self) -> asyncio.Queue[PriceEvent]:
        return self._queue

    def _tag(self) -> str:
        return "" if self._feed.primary else f"[{self._feed.exchange}] "

    async def on_ws_message(self, msg: dict, mono: float) -> None:
        result = self._feed.processor.apply_message(msg, mono=mono)
        if result is None:
            return
        events = result if isinstance(result, list) else [result]
        stats = self._feed.stats
        for event in events:
            stats.price_events_in += 1
            if self._queue.full():
                stats.price_queue_blocked_total += 1
            await self._queue.put(event)
            stats.price_queue_size = self._queue.qsize()

    def on_ws_connect(self, symbols: list[str]) -> None:
        log(
            logger,
            logging.INFO,
            f"{self._tag()}WS connected, symbols={len(symbols)}",
            component="market_worker",
            event="ws_connect",
            count=len(symbols),
        )
        processor = self._feed.processor
        if hasattr(processor, "on_resubscribe"):
            processor.on_resubscribe(symbols)
        if symbols and any(self._feed.history.span(s) for s in symbols[:5]):
            self._spawn(self._backfill_gaps_later(symbols), "gap-backfill")

    def on_ws_disconnect(self, symbols: list[str], last_ts_ms: int | None) -> None:
        log(
            logger,
            logging.WARNING,
            f"{self._tag()}WS disconnected, symbols={len(symbols)} last_ts_ms={last_ts_ms}",
            component="market_worker",
            event="ws_disconnect",
            count=len(symbols),
        )
        self._feed.history.mark_disconnected(symbols, at_ts_ms=last_ts_ms)

    def process_event(self, event: PriceEvent) -> None:
        feed = self._feed
        stats = feed.stats
        try:
            feed.history.record(event.symbol, ts_ms=event.exchange_ts_ms, price=event.last_price)
            if event.kind is PriceEventKind.LIVE and feed.evaluate_admin:
                monitor = getattr(feed, "profile_monitor", None)
                if monitor is not None:
                    monitor.evaluate(event)
                else:
                    feed.signal_engine.evaluate(event)
        except Exception:
            metrics.record_error()
            logger.exception(
                "%sPrice event processing failed for %s", self._tag(), event.symbol
            )
        commercial = self._commercial_engine
        if commercial is not None and event.kind is PriceEventKind.LIVE:
            try:
                commercial.evaluate(event)
            except Exception:
                metrics.commercial_engine_errors_total += 1
                logger.exception(
                    "%sCommercial evaluation failed for %s", self._tag(), event.symbol
                )
        stats.price_events_processed += 1
        stats.price_queue_size = self._queue.qsize()
        lag = time.perf_counter() - event.received_mono
        stats.record_latency(lag)
        max_age = feed.signal_engine.settings.max_tick_age_ms
        if lag * 1000 > max_age:
            stats.late_price_events_total += 1

    async def start(self) -> None:
        feed = self._feed
        ws = feed.ws
        registry = feed.registry
        if ws is None or registry is None:
            raise RuntimeError("MarketWorker.start requires a registry and websocket")
        ws.on_message = self.on_ws_message
        ws.on_connect = self.on_ws_connect
        ws.on_disconnect = self.on_ws_disconnect
        registry._on_change = self._on_universe_change

        log(
            logger,
            logging.INFO,
            f"{self._tag()}Market worker starting",
            component="market_worker",
            event="start",
        )
        await registry.start()
        symbols = registry.symbols
        if hasattr(feed.processor, "set_universe"):
            feed.processor.set_universe(symbols)
        log(
            logger,
            logging.INFO,
            f"{self._tag()}Registry ready with {len(symbols)} symbols; starting WS",
            component="market_worker",
            event="registry_ready",
            count=len(symbols),
        )
        self._tasks.append(supervise(f"market-process-{feed.exchange}", self._process_loop))
        self._tasks.append(supervise(f"readiness-{feed.exchange}", self._readiness_loop))
        if feed.primary:
            self._tasks.append(supervise("loop-monitor", self._loop_monitor))
        await ws.start(symbols)
        self._spawn(self._warmup_symbols(symbols), "kline-warmup")

    async def stop(self) -> None:
        feed = self._feed
        if feed.ws is not None:
            await feed.ws.stop()
        if feed.registry is not None:
            await feed.registry.stop()
        await cancel_tasks(self._tasks)
        await cancel_tasks(list(self._background))
        if feed.correlator is not None:
            feed.correlator.flush_all()

    def _spawn(self, coro, name: str) -> None:
        task = asyncio.create_task(coro, name=f"{name}-{self._feed.exchange}")
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
        feed = self._feed
        symbols = [i.symbol for i in instruments]
        if hasattr(feed.processor, "set_universe"):
            feed.processor.set_universe(symbols)
        if feed.ws is not None:
            await feed.ws.update_symbols(symbols)
        for sym in removed:
            feed.processor.reset_symbol(sym)
            feed.signal_engine.dedup.clear_symbol(sym)
            if feed.notifications is not None:
                feed.notifications.forget_symbol(sym, feed.exchange)
            if self._commercial_engine is not None:
                self._commercial_engine.clear_symbol(sym)
        if added and len(added) < len(symbols):
            self._spawn(self._warmup_symbols(sorted(added)), "kline-warmup-added")
        if feed.repo is None:
            return
        try:
            await feed.repo.log_event(
                level="INFO",
                component="registry",
                event_type="universe_changed",
                message=(
                    f"{self._tag()}added={len(added)} removed={len(removed)} total={len(symbols)}"
                ),
                details={
                    "exchange": feed.exchange,
                    "added": sorted(added)[:50],
                    "removed": sorted(removed)[:50],
                },
            )
        except Exception:
            logger.warning("Failed to log universe change", exc_info=True)

    async def _readiness_loop(self) -> None:
        feed = self._feed
        was_ready: bool | None = None
        while True:
            ws = feed.ws
            age = None if ws is None else ws.last_message_age_sec
            fresh = age is not None and age < feed.stale_connection_sec
            connections = 0 if ws is None else ws.connection_count
            ready = bool(feed.stats.instruments_active > 0 and connections > 0 and fresh)
            feed.stats.ready = ready
            feed.stats.last_message_age_ms = None if age is None else round(age * 1000, 1)
            if feed.primary:
                metrics.ready = ready
                metrics.last_message_age_ms = feed.stats.last_message_age_ms
            if was_ready is None or ready != was_ready:
                log(
                    logger,
                    logging.INFO if ready else logging.WARNING,
                    f"{self._tag()}Screener ready={ready} "
                    f"instruments={feed.stats.instruments_active} "
                    f"ws={connections} last_msg_age_ms={feed.stats.last_message_age_ms}",
                    component="market_worker",
                    event="ready_changed",
                    status="ready" if ready else "not_ready",
                    count=feed.stats.instruments_active,
                )
                if not feed.primary and not ready and feed.repo is not None:
                    try:
                        await feed.repo.log_event(
                            level="WARNING",
                            component=feed.exchange,
                            event_type="feed_degraded",
                            message=(
                                f"{feed.exchange} feed not ready "
                                f"instruments={feed.stats.instruments_active} "
                                f"ws={connections}"
                            ),
                        )
                    except Exception:
                        logger.warning(
                            "Failed to log %s degradation", feed.exchange, exc_info=True
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
        settings = self._feed.signal_engine.settings
        longest = max(
            settings.long_interval_sec, settings.short_interval_sec, settings.dump_interval_sec
        )
        if self._commercial_engine is not None:
            longest = max(longest, MAX_RULE_INTERVAL_SEC)
        return min(1000, max(self._feed.kline_warmup_limit, longest // 60 + 4))

    async def _fetch_kline(self, symbol: str, what: str, **kwargs) -> list | None:
        """One REST budget per feed: every shard reconnecting at once shares it."""
        if self._kline_sem is None:
            self._kline_sem = asyncio.Semaphore(self._feed.kline_concurrency)
        for attempt in range(_KLINE_ATTEMPTS):
            async with self._kline_sem:
                try:
                    return await self._feed.rest.fetch_kline(symbol, interval="1", **kwargs)
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
            if attempt + 1 < _KLINE_ATTEMPTS:
                await asyncio.sleep(_KLINE_RETRY_SEC * (attempt + 1))
        logger.warning("%s%s failed for %s (%s)", self._tag(), what, symbol, error)
        return None

    async def _warmup_symbols(self, symbols: list[str]) -> None:
        if not symbols or self._feed.rest is None:
            return
        feed = self._feed
        limit = self._kline_limit()
        done = 0

        async def one(symbol: str) -> None:
            nonlocal done
            rows = await self._fetch_kline(symbol, "Kline warmup", limit=limit)
            if rows is None:
                return
            closes = normalize_klines(rows, now_ms=int(time.time() * 1000))
            feed.history.backfill_klines(symbol, closes)
            done += 1

        started = time.monotonic()
        await asyncio.gather(*(one(s) for s in symbols))
        logger.info(
            "%sKline warmup: %s/%s symbols in %.1fs",
            self._tag(),
            done,
            len(symbols),
            time.monotonic() - started,
        )

    async def _backfill_gaps_later(self, symbols: list[str]) -> None:
        await asyncio.sleep(_GAP_CHECK_DELAY_SEC)
        feed = self._feed
        if feed.rest is None:
            return
        min_len = feed.gap_backfill_min_sec

        async def one(symbol: str, gs: int, ge: int) -> None:
            rows = await self._fetch_kline(
                symbol,
                "Gap backfill",
                limit=min(1000, (ge - gs) // 60 + 4),
                start_ms=(gs - 120) * 1000,
                end_ms=ge * 1000,
            )
            if rows is None:
                return
            closes = normalize_klines(rows, now_ms=int(time.time() * 1000))
            feed.history.backfill_klines(symbol, closes, lo_sec=gs, hi_sec=ge)

        jobs = [
            one(symbol, gs, ge)
            for symbol in symbols
            for gs, ge in feed.history.gaps(symbol, min_len_sec=min_len)
        ]
        if jobs:
            logger.info("%sBackfilling %s history gaps from REST klines", self._tag(), len(jobs))
            await asyncio.gather(*jobs)
        rewarm = feed.history.take_needs_warmup()
        if rewarm:
            logger.info("%sRe-warming %s symbols after a long outage", self._tag(), len(rewarm))
            await self._warmup_symbols(rewarm)
