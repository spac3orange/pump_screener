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
    ) -> None:
        self._store = store
        self._maxsize = maxsize
        self._op_timeout = op_timeout_sec
        self._max_backoff = max_backoff_sec
        self._items: deque[MarketSignal | _Delivery] = deque()
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    @property
    def backlog(self) -> int:
        return len(self._items)

    def _push(self, item: MarketSignal | _Delivery) -> None:
        if self._store is None:
            return
        if len(self._items) >= self._maxsize:
            self._items.popleft()
            metrics.persistence_drops_total += 1
        self._items.append(item)
        self._wake.set()

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
                logger.warning("Persistence failed; retrying in %.1fs", backoff, exc_info=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._max_backoff)
                continue
            backoff = 0.5
            if self._items and self._items[0] is item:
                self._items.popleft()
