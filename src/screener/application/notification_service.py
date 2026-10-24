from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from screener.application.metrics import metrics
from screener.application.persistence_service import PersistenceService
from screener.application.supervisor import cancel_tasks, supervise
from screener.domain.enums import DeliveryStatus
from screener.domain.models import MarketSignal

logger = logging.getLogger(__name__)

SendFunc = Callable[..., Awaitable[None]]


class SignalCounter(Protocol):
    async def count_symbol_signals_24h(
        self,
        symbol: str,
        *,
        now: datetime,
        exclude_id: UUID | None = None,
        exchange: str = "bybit",
    ) -> int: ...


def recent_key(symbol: str, exchange: str = "bybit") -> str:
    return f"{exchange}:{symbol}"


class RecentSignals:
    """In-memory per-symbol signal timestamps for the last 24 hours.

    Keys are ``exchange:symbol`` so the same ticker on two exchanges does not mix.
    """

    _SWEEP_EVERY = 256

    def __init__(self) -> None:
        self._by_symbol: dict[str, deque[datetime]] = defaultdict(deque)
        self._records = 0

    def __len__(self) -> int:
        return len(self._by_symbol)

    def record(self, symbol: str, at: datetime) -> None:
        self._by_symbol[symbol].append(at)
        self._records += 1
        if self._records % self._SWEEP_EVERY == 0:
            self.sweep(at)

    def count(self, symbol: str, now: datetime) -> int:
        items = self._by_symbol.get(symbol)
        if not items:
            return 0
        since = now - timedelta(hours=24)
        while items and items[0] < since:
            items.popleft()
        if not items:
            del self._by_symbol[symbol]
            return 0
        return len(items)

    def sweep(self, now: datetime) -> None:
        since = now - timedelta(hours=24)
        for symbol in list(self._by_symbol):
            items = self._by_symbol[symbol]
            while items and items[0] < since:
                items.popleft()
            if not items:
                del self._by_symbol[symbol]

    def forget(self, symbol: str) -> None:
        self._by_symbol.pop(symbol, None)


class NotificationService:
    """Bounded queue between signal dispatch and Telegram delivery.

    Independent of persistence: Signal 24h uses a bounded DB query and falls
    back to an in-memory counter, and delivery results are handed to the
    persistence writer instead of being written inline.
    """

    def __init__(
        self,
        *,
        chat_ids: Sequence[int],
        counter: SignalCounter | None = None,
        persistence: PersistenceService | None = None,
        send_func: SendFunc | None = None,
        maxsize: int = 1000,
        count_timeout_sec: float = 2.0,
    ) -> None:
        self._chat_ids = list(chat_ids)
        self._counter = counter
        self._persistence = persistence
        self._send_func = send_func
        self._count_timeout = count_timeout_sec
        self._queue: asyncio.Queue[MarketSignal] = asyncio.Queue(maxsize=maxsize)
        self._recent = RecentSignals()
        self._tasks: list[asyncio.Task] = []

    def set_send_func(self, send_func: SendFunc) -> None:
        self._send_func = send_func

    def set_chat_ids(self, chat_ids: Sequence[int]) -> None:
        self._chat_ids = list(chat_ids)

    def forget_symbol(self, symbol: str, exchange: str = "bybit") -> None:
        self._recent.forget(recent_key(symbol, exchange))

    async def start(self) -> None:
        self._tasks.append(supervise("notifications", self._loop))

    async def stop(self, drain_timeout_sec: float = 5.0) -> None:
        # Signals flushed by the correlators on shutdown are still worth sending.
        if self._tasks and not self._queue.empty():
            try:
                await asyncio.wait_for(self._queue.join(), timeout=drain_timeout_sec)
            except TimeoutError:
                logger.warning("Shutdown: %s alerts left unsent", self._queue.qsize())
        await cancel_tasks(self._tasks)

    def enqueue(self, signal: MarketSignal) -> None:
        self._recent.record(recent_key(signal.symbol, signal.exchange), signal.detected_at)
        if not self._chat_ids or self._send_func is None:
            return
        if self._queue.full():
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except asyncio.QueueEmpty:
                pass
            metrics.notification_drops_total += 1
            logger.warning("Notification queue full; dropped oldest alert")
        self._queue.put_nowait(signal)

    async def _loop(self) -> None:
        while True:
            signal = await self._queue.get()
            try:
                await self._deliver(signal)
            finally:
                self._queue.task_done()

    async def signals_24h(self, signal: MarketSignal) -> int:
        in_memory = self._recent.count(
            recent_key(signal.symbol, signal.exchange), signal.detected_at
        )
        from_db = 0
        if self._counter is not None:
            try:
                from_db = 1 + await asyncio.wait_for(
                    self._counter.count_symbol_signals_24h(
                        signal.symbol,
                        now=signal.detected_at,
                        exclude_id=signal.id,
                        exchange=signal.exchange,
                    ),
                    timeout=self._count_timeout,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Signal 24h count fell back to memory", exc_info=True)
        return max(1, in_memory, from_db)

    async def _deliver(self, signal: MarketSignal) -> None:
        assert self._send_func is not None
        signals_24h = await self.signals_24h(signal)
        for chat_id in self._chat_ids:
            try:
                await self._send_func(chat_id, signal, signals_24h=signals_24h)
                status, error = DeliveryStatus.SENT, None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                metrics.record_error()
                logger.exception("Telegram delivery failed chat_id=%s", chat_id)
                status, error = DeliveryStatus.FAILED, str(exc)
            if self._persistence is not None:
                self._persistence.enqueue_delivery(signal.id, chat_id, status, error)
