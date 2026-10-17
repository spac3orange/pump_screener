"""Fan-out of group signals to subscribers and rate-limited Telegram delivery.

Never on the market path: ``on_group_signal`` only appends to a bounded queue.
Access is checked against the real clock right before every send, so delivery
stops the moment access ends even if the expiration worker has not run yet.
Telegram limits (~30 msg/s per bot, ~1 msg/s per chat) are respected with a
global pacer and a per-chat minimum interval; signals that queue up for one
user while it waits are merged into one message.
"""

from __future__ import annotations

import asyncio
import heapq
import logging
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime

from screener.application.clock import Clock
from screener.application.commercial.registry import ActiveUserRegistry
from screener.application.commercial.signal_writer import CommercialSignalWriter
from screener.application.metrics import metrics
from screener.application.notification_service import RecentSignals
from screener.application.supervisor import cancel_tasks, supervise
from screener.config import CommercialSettings
from screener.domain.commercial import DeliveryOutcome, GroupSignal, UserSignalRecord

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 3
_SEEN_MAX = 20_000


class DeliveryBlocked(Exception):
    """The user blocked the bot or the chat no longer exists."""


class DeliveryRetryAfter(Exception):
    def __init__(self, seconds: float) -> None:
        super().__init__(f"retry after {seconds}s")
        self.seconds = seconds


SendText = Callable[[int, str], Awaitable[None]]
FormatSignal = Callable[[GroupSignal, int], str]
BlockedHandler = Callable[[int], Awaitable[None]]


@dataclass(slots=True)
class _Item:
    signal: GroupSignal
    text: str
    attempts: int = 0


@dataclass(slots=True)
class _UserQueue:
    items: deque[_Item] = field(default_factory=deque)
    next_at: float = 0.0
    scheduled: bool = False
    failures: int = 0


class UserDispatcher:
    def __init__(
        self,
        registry: ActiveUserRegistry,
        writer: CommercialSignalWriter,
        clock: Clock,
        settings: CommercialSettings,
        *,
        format_signal: FormatSignal,
        send: SendText | None = None,
        on_blocked: BlockedHandler | None = None,
        footer: str = "",
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._registry = registry
        self._writer = writer
        self._clock = clock
        self._settings = settings
        self._format = format_signal
        self._send = send
        self._on_blocked = on_blocked
        self._footer = footer
        self._mono = monotonic
        self._fanout: deque[GroupSignal] = deque()
        self._fanout_wake = asyncio.Event()
        self._queues: dict[int, _UserQueue] = {}
        self._heap: list[tuple[float, int, int]] = []
        self._seq = 0
        self._send_wake = asyncio.Event()
        self._global_next = 0.0
        self._pause_until = 0.0
        self._recent = RecentSignals()
        self._seen: OrderedDict[object, None] = OrderedDict()
        self._tasks: list[asyncio.Task] = []
        self._background: set[asyncio.Task] = set()

    def set_send(self, send: SendText) -> None:
        self._send = send

    def set_on_blocked(self, handler: BlockedHandler) -> None:
        self._on_blocked = handler

    @property
    def queued_messages(self) -> int:
        return sum(len(q.items) for q in self._queues.values())

    # --- intake (sync, called from the correlator) ---------------------------------

    def on_group_signal(self, signal: GroupSignal) -> None:
        if signal.id in self._seen:
            return
        self._seen[signal.id] = None
        if len(self._seen) > _SEEN_MAX:
            self._seen.popitem(last=False)
        self._writer.enqueue_signal(signal)
        if len(self._fanout) >= self._settings.fanout_queue_maxsize:
            self._fanout.popleft()
            metrics.user_fanout_drops_total += 1
        self._fanout.append(signal)
        metrics.user_fanout_queue_size = len(self._fanout)
        self._fanout_wake.set()

    # --- lifecycle ------------------------------------------------------------------

    async def start(self) -> None:
        self._tasks.append(supervise("user-fanout", self._fanout_loop))
        self._tasks.append(supervise("user-sender", self._sender_loop))

    async def stop(self) -> None:
        await cancel_tasks(self._tasks)
        await cancel_tasks(list(self._background))

    # --- fan-out ----------------------------------------------------------------------

    async def _fanout_loop(self) -> None:
        while True:
            if not self._fanout:
                self._fanout_wake.clear()
                await self._fanout_wake.wait()
                continue
            self.fanout_once()
            # Yield between signals so a huge group never monopolises the loop.
            await asyncio.sleep(0)

    def fanout_once(self) -> int:
        """Expand one queued group signal into per-user queues; returns users queued."""
        if not self._fanout:
            return 0
        signal = self._fanout.popleft()
        metrics.user_fanout_queue_size = len(self._fanout)
        members = self._registry.members(signal.group_key)
        if not members:
            return 0
        recent_key = f"{signal.group_key}:{signal.symbol}"
        self._recent.record(recent_key, signal.detected_at)
        text = self._format(signal, self._recent.count(recent_key, signal.detected_at))
        now = self._clock.now()
        queued = 0
        for user_id in members:
            if not self._registry.has_access(user_id, now):
                self._record(user_id, signal, DeliveryOutcome.DROPPED_ACCESS, "no_access", 0, now)
                continue
            q = self._queues.get(user_id)
            if q is None:
                q = self._queues[user_id] = _UserQueue()
            if len(q.items) >= self._settings.user_queue_maxsize:
                old = q.items.popleft()
                self._record(
                    user_id, old.signal, DeliveryOutcome.DROPPED_OVERFLOW, "overflow", 0, now
                )
            q.items.append(_Item(signal, text))
            self._schedule(user_id, q, max(self._mono(), q.next_at))
            queued += 1
        self._update_queue_metric()
        return queued

    # --- sending ----------------------------------------------------------------------

    def _schedule(self, user_id: int, q: _UserQueue, due: float) -> None:
        if q.scheduled:
            return
        q.scheduled = True
        self._seq += 1
        heapq.heappush(self._heap, (due, self._seq, user_id))
        self._send_wake.set()

    async def _sender_loop(self) -> None:
        while True:
            if not self._heap:
                self._send_wake.clear()
                await self._send_wake.wait()
                continue
            due = max(self._heap[0][0], self._pause_until, self._global_next)
            delay = due - self._mono()
            if delay > 0:
                self._send_wake.clear()
                try:
                    await asyncio.wait_for(self._send_wake.wait(), timeout=delay)
                except TimeoutError:
                    pass
                continue
            await self.send_next()

    async def send_next(self) -> bool:
        """Send one batch for the most overdue user; returns whether anything was due."""
        if not self._heap:
            return False
        _, _, user_id = heapq.heappop(self._heap)
        q = self._queues.get(user_id)
        if q is None:
            return True
        q.scheduled = False
        await self._send_batch(user_id, q)
        if not q.items and not q.scheduled:
            self._queues.pop(user_id, None)
        self._update_queue_metric()
        return True

    async def _send_batch(self, user_id: int, q: _UserQueue) -> None:
        now = self._clock.now()
        entry = self._registry.get(user_id)
        if entry is None or not self._registry.has_access(user_id, now):
            self._drain(user_id, q, DeliveryOutcome.DROPPED_ACCESS, "no_access", now)
            return
        max_age = self._settings.user_signal_max_age_sec
        while q.items and _age_sec(q.items[0].signal.detected_at, now) > max_age:
            old = q.items.popleft()
            self._record(
                user_id, old.signal, DeliveryOutcome.DROPPED_EXPIRED, "max_age", old.attempts, now
            )
        if not q.items:
            return
        batch = [q.items.popleft()]
        text = batch[0].text + self._footer
        for item in batch:
            item.attempts += 1
        mono = self._mono()
        self._global_next = (
            max(mono, self._global_next) + 1.0 / self._settings.user_send_rate_per_sec
        )
        try:
            if self._send is None:
                raise RuntimeError("Telegram sender is not configured")
            await self._send(entry.chat_id, text)
        except asyncio.CancelledError:
            q.items.extendleft(reversed(batch))
            raise
        except DeliveryBlocked:
            metrics.user_bot_blocked_total += 1
            for item in batch:
                self._record(
                    user_id,
                    item.signal,
                    DeliveryOutcome.DROPPED_BLOCKED,
                    "blocked",
                    item.attempts,
                    now,
                )
            self._drain(user_id, q, DeliveryOutcome.DROPPED_BLOCKED, "blocked", now)
            self._registry.remove(user_id)
            if self._on_blocked is not None:
                self._spawn(self._on_blocked(user_id))
            return
        except DeliveryRetryAfter as exc:
            metrics.user_flood_waits_total += 1
            self._pause_until = self._mono() + max(exc.seconds, 1.0)
            q.items.extendleft(reversed(batch))
            self._schedule(user_id, q, self._pause_until)
            logger.warning("Telegram flood control on user delivery: pause %.0fs", exc.seconds)
            return
        except Exception as exc:
            q.failures += 1
            metrics.user_send_retries_total += 1
            if q.failures >= _MAX_ATTEMPTS:
                q.failures = 0
                for item in batch:
                    self._record(
                        user_id,
                        item.signal,
                        DeliveryOutcome.FAILED,
                        type(exc).__name__[:32],
                        item.attempts,
                        now,
                    )
                logger.warning("User delivery failed after %s attempts", _MAX_ATTEMPTS)
            else:
                q.items.extendleft(reversed(batch))
                self._schedule(user_id, q, self._mono() + 2.0**q.failures)
                return
        else:
            q.failures = 0
            metrics.user_messages_sent_total += 1
            sent_at = self._clock.now()
            for item in batch:
                self._record(
                    user_id,
                    item.signal,
                    DeliveryOutcome.SENT,
                    None,
                    item.attempts,
                    sent_at,
                    sent=True,
                )
        q.next_at = self._mono() + self._settings.user_chat_min_interval_sec
        if q.items:
            self._schedule(user_id, q, q.next_at)

    # --- helpers ----------------------------------------------------------------------

    def _drain(
        self, user_id: int, q: _UserQueue, outcome: DeliveryOutcome, code: str, now: datetime
    ) -> None:
        while q.items:
            item = q.items.popleft()
            self._record(user_id, item.signal, outcome, code, item.attempts, now)

    def _record(
        self,
        user_id: int,
        signal: GroupSignal,
        outcome: DeliveryOutcome,
        code: str | None,
        attempts: int,
        now: datetime,
        *,
        sent: bool = False,
    ) -> None:
        metrics.record_delivery(outcome.value)
        self._writer.enqueue_record(
            UserSignalRecord(
                user_id=user_id,
                signal_id=signal.id,
                status=outcome.value,
                error_code=code,
                attempts=attempts,
                created_at=now,
                sent_at=now if sent else None,
            )
        )

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _update_queue_metric(self) -> None:
        metrics.user_delivery_queue_size = self.queued_messages


def _age_sec(detected_at: datetime, now: datetime) -> float:
    return (now - detected_at).total_seconds()
