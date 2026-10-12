from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

from screener.application.metrics import metrics
from screener.domain.enums import DeliveryStatus
from screener.domain.models import SignalEvent
from screener.infrastructure.db.repositories import Repository

logger = logging.getLogger(__name__)


class NotificationService:
    """Bounded queue between signal engine and Telegram delivery."""

    def __init__(
        self,
        *,
        chat_ids: Sequence[int],
        repo: Repository | None = None,
        send_func=None,
        maxsize: int = 1000,
    ) -> None:
        self._chat_ids = list(chat_ids)
        self._repo = repo
        self._send_func = send_func
        self._queue: asyncio.Queue[SignalEvent] = asyncio.Queue(maxsize=maxsize)
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def set_send_func(self, send_func) -> None:
        self._send_func = send_func

    def set_chat_ids(self, chat_ids: Sequence[int]) -> None:
        self._chat_ids = list(chat_ids)

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="notification-service")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def enqueue(self, signal: SignalEvent) -> None:
        if not self._chat_ids or self._send_func is None:
            return
        try:
            self._queue.put_nowait(signal)
        except asyncio.QueueFull:
            try:
                _ = self._queue.get_nowait()
                self._queue.task_done()
            except asyncio.QueueEmpty:
                pass
            metrics.record_drop()
            try:
                self._queue.put_nowait(signal)
            except asyncio.QueueFull:
                metrics.record_drop()
                logger.warning("Notification queue full; dropping signal %s", signal.symbol)

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                signal = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            try:
                await self._deliver(signal)
            finally:
                self._queue.task_done()

    async def _deliver(self, signal: SignalEvent) -> None:
        assert self._send_func is not None
        signals_24h = 1
        if self._repo is not None:
            signals_24h = await self._repo.count_symbol_signals_24h(
                signal.symbol, now=signal.detected_at
            )
        for chat_id in self._chat_ids:
            try:
                await self._send_func(chat_id, signal, signals_24h=signals_24h)
                if self._repo is not None:
                    await self._repo.save_notification(
                        signal.id, chat_id, DeliveryStatus.SENT
                    )
            except Exception as exc:
                metrics.record_error()
                logger.exception("Telegram delivery failed chat_id=%s", chat_id)
                if self._repo is not None:
                    await self._repo.save_notification(
                        signal.id, chat_id, DeliveryStatus.FAILED, error=str(exc)
                    )
