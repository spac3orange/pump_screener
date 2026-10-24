from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from screener.application.metrics import metrics
from screener.application.supervisor import cancel_tasks, supervise
from screener.domain.enums import DeliveryStatus
from screener.domain.models import MarketSignal

logger = logging.getLogger(__name__)

_LOST_SIGNALS_MAX = 10_000


class SignalStore(Protocol):
    async def save_signal(self, signal: MarketSignal) -> None: ...

    async def save_notification(
        self,
        signal_id: UUID,
        chat_id: int,
        status: DeliveryStatus,
        error: str | None = None,
    ) -> None: ...


@dataclass(slots=True, frozen=True)
class _Delivery:
    signal_id: UUID
    chat_id: int
    status: DeliveryStatus
    error: str | None


class PersistenceService:
    """Ordered, retrying writer for signals and delivery records.

    Runs off the market path. Deliveries are queued behind their signal, so the
    notification_history foreign key is always satisfied. A slow or unavailable
    database only grows this bounded backlog; it never blocks alerts.
    """

    def __init__(
        self,
        store: SignalStore | None,
        *,
        maxsize: int = 10_000,
        op_timeout_sec: float = 10.0,
        max_backoff_sec: float = 30.0,
        max_attempts: int = 20,
    ) -> None:
        self._store = store
        self._maxsize = maxsize
        self._op_timeout = op_timeout_sec
        self._max_backoff = max_backoff_sec
        self._max_attempts = max_attempts
        self._items: deque[MarketSignal | _Delivery] = deque()
        # Deliveries of a signal that was never written would violate the FK forever.
        self._lost_signals: deque[UUID] = deque(maxlen=_LOST_SIGNALS_MAX)
        self._lost_signal_set: set[UUID] = set()
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    @property
    def backlog(self) -> int:
        return len(self._items)

    def _push(self, item: MarketSignal | _Delivery) -> None:
        if self._store is None:
            return
        if isinstance(item, _Delivery) and item.signal_id in self._lost_signal_set:
            metrics.persistence_drops_total += 1
            return
        if len(self._items) >= self._maxsize:
            self._drop(self._items.popleft())
        self._items.append(item)
        self._wake.set()

    def _drop(self, item: MarketSignal | _Delivery) -> None:
        metrics.persistence_drops_total += 1
        if not isinstance(item, MarketSignal):
            return
        if len(self._lost_signals) == self._lost_signals.maxlen:
            self._lost_signal_set.discard(self._lost_signals[0])
        self._lost_signals.append(item.id)
        self._lost_signal_set.add(item.id)
        self._items = deque(
            i
            for i in self._items
            if not (isinstance(i, _Delivery) and i.signal_id == item.id)
        )

    def enqueue_signal(self, signal: MarketSignal) -> None:
        self._push(signal)

    def enqueue_delivery(
        self,
        signal_id: UUID,
        chat_id: int,
        status: DeliveryStatus,
        error: str | None = None,
    ) -> None:
        self._push(_Delivery(signal_id, chat_id, status, error))

    async def start(self) -> None:
        self._tasks.append(supervise("persistence", self._loop))

    async def stop(self) -> None:
        await self.drain(timeout_sec=5.0)
        await cancel_tasks(self._tasks)

    async def drain(self, *, timeout_sec: float) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_sec
        while self._items and loop.time() < deadline:
            await asyncio.sleep(0.05)

    async def _write(self, item: MarketSignal | _Delivery) -> None:
        assert self._store is not None
        if isinstance(item, MarketSignal):
            await self._store.save_signal(item)
        else:
            await self._store.save_notification(
                item.signal_id, item.chat_id, item.status, error=item.error
            )

    async def _loop(self) -> None:
        backoff = 0.5
        attempts = 0
        while True:
            if not self._items:
                self._wake.clear()
                await self._wake.wait()
                continue
            item = self._items[0]
            try:
                await asyncio.wait_for(self._write(item), timeout=self._op_timeout)
            except asyncio.CancelledError:
                raise
            except Exception:
                metrics.persistence_failures_total += 1
                attempts += 1
                if attempts >= self._max_attempts:
                    logger.error(
                        "Persistence gave up on %s after %d attempts",
                        type(item).__name__,
                        attempts,
                        exc_info=True,
                    )
                    if self._items and self._items[0] is item:
                        self._items.popleft()
                        self._drop(item)
                    attempts = 0
                    continue
                logger.warning("Persistence failed; retrying in %.1fs", backoff, exc_info=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._max_backoff)
                continue
            backoff = 0.5
            attempts = 0
            if self._items and self._items[0] is item:
                self._items.popleft()
