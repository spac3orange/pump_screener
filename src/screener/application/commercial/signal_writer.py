from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Iterable
from typing import Protocol
from uuid import UUID

from screener.application.metrics import metrics
from screener.application.supervisor import cancel_tasks, supervise
from screener.domain.commercial import GroupSignal, UserSignalRecord

logger = logging.getLogger(__name__)

_DROPPED_IDS_MAX = 10_000


class CommercialSignalStore(Protocol):
    async def save_commercial_signals(self, signals: Iterable[GroupSignal]) -> None: ...

    async def save_user_signal_events(self, records: Iterable[UserSignalRecord]) -> None: ...


class CommercialSignalWriter:
    """Ordered, batched, retrying writer for commercial signals and per-user outcomes.

    Off the market and delivery paths: a slow database only grows this bounded
    backlog. A signal is always written before the outcome rows that reference it.
    """

    def __init__(
        self,
        store: CommercialSignalStore | None,
        *,
        maxsize: int = 50_000,
        batch_size: int = 500,
        op_timeout_sec: float = 10.0,
        max_backoff_sec: float = 30.0,
    ) -> None:
        self._store = store
        self._maxsize = maxsize
        self._batch = batch_size
        self._op_timeout = op_timeout_sec
        self._max_backoff = max_backoff_sec
        self._items: deque[GroupSignal | UserSignalRecord] = deque()
        self._dropped_signals: set[UUID] = set()
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    @property
    def backlog(self) -> int:
        return len(self._items)

    def enqueue_signal(self, signal: GroupSignal) -> None:
        self._push(signal)

    def enqueue_record(self, record: UserSignalRecord) -> None:
        self._push(record)

    def _push(self, item: GroupSignal | UserSignalRecord) -> None:
        if self._store is None:
            return
        if len(self._items) >= self._maxsize:
            dropped = self._items.popleft()
            metrics.user_writer_drops_total += 1
            if isinstance(dropped, GroupSignal):
                if len(self._dropped_signals) >= _DROPPED_IDS_MAX:
                    self._dropped_signals.clear()
                self._dropped_signals.add(dropped.id)
        self._items.append(item)
        self._wake.set()

    async def start(self) -> None:
        self._tasks.append(supervise("commercial-writer", self._loop))

    async def stop(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        while self._items and loop.time() < deadline:
            await asyncio.sleep(0.05)
        await cancel_tasks(self._tasks)

    async def flush_once(self) -> int:
        """Write one batch; returns how many items were persisted."""
        assert self._store is not None
        batch = [self._items[i] for i in range(min(self._batch, len(self._items)))]
        signals = [i for i in batch if isinstance(i, GroupSignal)]
        records = [
            i
            for i in batch
            if isinstance(i, UserSignalRecord) and i.signal_id not in self._dropped_signals
        ]
        if signals:
            await asyncio.wait_for(
                self._store.save_commercial_signals(signals), timeout=self._op_timeout
            )
        if records:
            await asyncio.wait_for(
                self._store.save_user_signal_events(records), timeout=self._op_timeout
            )
        for item in batch:
            # Overflow may have evicted items from the head while we were writing.
            if self._items and self._items[0] is item:
                self._items.popleft()
        return len(batch)

    async def _loop(self) -> None:
        backoff = 0.5
        while True:
            if not self._items:
                self._wake.clear()
                await self._wake.wait()
                continue
            try:
                await self.flush_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                metrics.user_writer_failures_total += 1
                logger.warning(
                    "Commercial writer failed; retrying in %.1fs", backoff, exc_info=True
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._max_backoff)
                continue
            backoff = 0.5
