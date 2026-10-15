"""Ends subscriptions and trials on time and notifies each user once per access period.

The claim (``expiry_notified_until = access_until``) is a single UPDATE, so a
restart never repeats a notification and access periods that ended while the
service was down are still notified on the next pass. Delivery itself does not
depend on this worker: the dispatcher checks ``access_until`` before every send.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta

from screener.application.clock import Clock
from screener.application.commercial.user_service import UserService
from screener.application.metrics import metrics
from screener.application.supervisor import cancel_tasks, supervise
from screener.domain.commercial import UserAccount

logger = logging.getLogger(__name__)

ExpiryNotify = Callable[[UserAccount], Awaitable[None]]

_PURGE_EVERY_SEC = 3600.0


class ExpirationWorker:
    def __init__(
        self,
        store,
        users: UserService,
        clock: Clock,
        *,
        notify: ExpiryNotify | None = None,
    ) -> None:
        self._store = store
        self._users = users
        self._clock = clock
        self._notify = notify
        self._tasks: list[asyncio.Task] = []
        self._last_purge_ms: int | None = None

    def set_notify(self, notify: ExpiryNotify) -> None:
        self._notify = notify

    async def start(self) -> None:
        self._tasks.append(supervise("commercial-expiry", self._loop))

    async def stop(self) -> None:
        await cancel_tasks(self._tasks)

    async def run_once(self) -> int:
        settings = self._users.settings
        now = self._clock.now()
        expired = await self._store.expire_subscriptions(now, self._users.grace)
        if expired:
            logger.info("Subscriptions expired: %s", expired)
        claimed = await self._store.claim_expired(now)
        for user in claimed:
            await self._users.apply_account(user)
            if self._notify is None:
                continue
            try:
                await self._notify(user)
                metrics.expiry_notifications_total += 1
            except Exception as exc:
                logger.info("Expiry notice to user_id=%s failed: %s", user.id, type(exc).__name__)
        await self._maybe_purge(settings.user_signal_retention_days)
        return len(claimed)

    async def _maybe_purge(self, retention_days: int) -> None:
        now_ms = self._clock.time_ms()
        if (
            self._last_purge_ms is not None
            and now_ms - self._last_purge_ms < _PURGE_EVERY_SEC * 1000
        ):
            return
        self._last_purge_ms = now_ms
        try:
            removed = await self._store.purge_signals_before(
                self._clock.now() - timedelta(days=retention_days)
            )
            if removed:
                logger.info("Purged %s commercial signals older than %sd", removed, retention_days)
        except Exception:
            logger.warning("Commercial signal purge failed", exc_info=True)

    async def _loop(self) -> None:
        interval = self._users.settings.expiry_check_sec
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Expiration pass failed", exc_info=True)
            await asyncio.sleep(interval)
