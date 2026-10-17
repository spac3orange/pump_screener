from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from screener.application.clock import FakeClock
from screener.application.commercial.dispatcher import (
    DeliveryBlocked,
    DeliveryRetryAfter,
    UserDispatcher,
)
from screener.application.commercial.registry import ActiveUserRegistry, UserEntry
from screener.application.commercial.signal_writer import CommercialSignalWriter
from screener.config import CommercialSettings
from screener.domain.commercial import GroupSignal, UserRole
from screener.domain.enums import Direction, HistoryQuality, SignalType
from screener.domain.models import RuleMatch
from tests.helpers import settings

T0 = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
CFG = settings()
KEY = CFG.config_hash()


class FakeWriter:
    def __init__(self) -> None:
        self.signals: list[GroupSignal] = []
        self.records = []

    def enqueue_signal(self, signal) -> None:
        self.signals.append(signal)

    def enqueue_record(self, record) -> None:
        self.records.append(record)

    def statuses(self, user_id: int | None = None) -> list[str]:
        return [r.status for r in self.records if user_id is None or r.user_id == user_id]


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.errors: list[Exception] = []

    async def __call__(self, chat_id: int, text: str) -> None:
        if self.errors:
            raise self.errors.pop(0)
        self.sent.append((chat_id, text))


def _signal(symbol: str = "AUSDT", at: datetime = T0) -> GroupSignal:
    m = RuleMatch(SignalType.PUMP, 60, 5.0, 6.0, 1.0, 1.06, 0, HistoryQuality.LIVE)
    return GroupSignal(KEY, symbol, Direction.UP, m, (m,), at)


def _setup(*, until=T0 + timedelta(hours=1), users=(1,), **cfg):
    clock = FakeClock(int(T0.timestamp() * 1000))
    registry = ActiveUserRegistry()
    registry.replace_all([(UserEntry(u, 1000 + u, UserRole.USER, until, KEY), CFG) for u in users])
    writer = FakeWriter()
    sender = FakeSender()
    blocked: list[int] = []

    async def on_blocked(uid: int) -> None:
        blocked.append(uid)

    base = dict(user_chat_min_interval_sec=0.0, user_send_rate_per_sec=1000.0)
    base.update(cfg)
    dispatcher = UserDispatcher(
        registry,
        writer,  # type: ignore[arg-type]
        clock,
        CommercialSettings(**base),
        format_signal=lambda s, n: f"{s.symbol}#{n}",
        send=sender,
        on_blocked=on_blocked,
        footer="\nPRO",
    )
    return dispatcher, registry, writer, sender, clock, blocked


async def test_delivers_to_members_with_access_only() -> None:
    d, registry, writer, sender, clock, _ = _setup(users=(1, 2))
    registry.upsert(UserEntry(2, 1002, UserRole.USER, T0, KEY), CFG)  # already expired
    d.on_group_signal(_signal())
    assert d.fanout_once() == 1
    assert await d.send_next()
    assert sender.sent == [(1001, "AUSDT#1\nPRO")]
    assert writer.statuses(1) == ["sent"] and writer.statuses(2) == ["dropped_access"]
    assert len(writer.signals) == 1


async def test_access_end_stops_delivery_without_expiration_worker() -> None:
    d, _, writer, sender, clock, _ = _setup(until=T0 + timedelta(seconds=30))
    d.on_group_signal(_signal())
    d.fanout_once()
    clock.advance(30_000)
    await d.send_next()
    assert sender.sent == []
    assert writer.statuses() == ["dropped_access"]


async def test_queued_signals_are_sent_one_per_message_in_order() -> None:
    d, _, writer, sender, _, _ = _setup()
    for sym in ("AUSDT", "BUSDT", "CUSDT"):
        d.on_group_signal(_signal(sym))
        d.fanout_once()
    for _ in range(3):
        await d.send_next()
    assert [t for _, t in sender.sent] == ["AUSDT#1\nPRO", "BUSDT#1\nPRO", "CUSDT#1\nPRO"]
    assert writer.statuses() == ["sent"] * 3


async def test_stale_signals_are_dropped() -> None:
    d, _, writer, sender, clock, _ = _setup(user_signal_max_age_sec=60)
    d.on_group_signal(_signal())
    d.fanout_once()
    clock.advance(61_000)
    await d.send_next()
    assert sender.sent == [] and writer.statuses() == ["dropped_expired"]


async def test_per_user_queue_overflow_drops_oldest() -> None:
    d, _, writer, sender, _, _ = _setup(user_queue_maxsize=2)
    for sym in ("AUSDT", "BUSDT", "CUSDT"):
        d.on_group_signal(_signal(sym))
        d.fanout_once()
    await d.send_next()
    await d.send_next()
    assert [t for _, t in sender.sent] == ["BUSDT#1\nPRO", "CUSDT#1\nPRO"]
    assert sorted(writer.statuses()) == ["dropped_overflow", "sent", "sent"]


async def test_blocked_bot_removes_user_and_marks_blocked() -> None:
    d, registry, writer, sender, _, blocked = _setup()
    sender.errors.append(DeliveryBlocked())
    d.on_group_signal(_signal())
    d.fanout_once()
    await d.send_next()
    await asyncio.sleep(0)
    assert registry.get(1) is None and blocked == [1]
    assert writer.statuses() == ["dropped_blocked"]


async def test_retry_after_pauses_and_keeps_items() -> None:
    d, _, writer, sender, _, _ = _setup()
    sender.errors.append(DeliveryRetryAfter(3))
    d.on_group_signal(_signal())
    d.fanout_once()
    await d.send_next()
    assert sender.sent == [] and writer.statuses() == []
    assert d._pause_until > 0
    await d.send_next()
    assert len(sender.sent) == 1 and writer.statuses() == ["sent"]
    assert writer.records[0].attempts == 2


async def test_transient_errors_retry_then_fail() -> None:
    d, _, writer, sender, _, _ = _setup()
    sender.errors.extend([RuntimeError("x"), RuntimeError("x"), RuntimeError("x")])
    d.on_group_signal(_signal())
    d.fanout_once()
    for _ in range(3):
        await d.send_next()
    assert sender.sent == []
    assert writer.statuses() == ["failed"]
    assert writer.records[0].error_code == "RuntimeError"


async def test_same_signal_is_fanned_out_once() -> None:
    d, _, writer, sender, _, _ = _setup()
    s = _signal()
    d.on_group_signal(s)
    d.on_group_signal(s)
    assert d.fanout_once() == 1
    assert d.fanout_once() == 0
    assert len(writer.signals) == 1


async def test_signal_24h_counter_is_per_group_and_symbol() -> None:
    d, _, _, sender, _, _ = _setup()
    for _ in range(2):
        d.on_group_signal(_signal())
        d.fanout_once()
        await d.send_next()
    assert [t.split("\n")[0] for _, t in sender.sent] == ["AUSDT#1", "AUSDT#2"]


async def test_background_loops_deliver() -> None:
    d, _, writer, sender, _, _ = _setup()
    await d.start()
    try:
        d.on_group_signal(_signal())
        for _ in range(100):
            if sender.sent:
                break
            await asyncio.sleep(0.01)
    finally:
        await d.stop()
    assert len(sender.sent) == 1


async def test_writer_persists_signal_before_records() -> None:
    calls: list[str] = []

    class Store:
        async def save_commercial_signals(self, signals) -> None:
            calls.append(f"signals:{len(list(signals))}")

        async def save_user_signal_events(self, records) -> None:
            calls.append(f"records:{len(list(records))}")

    writer = CommercialSignalWriter(Store())
    d, *_ = _setup()
    s = _signal()
    writer.enqueue_signal(s)
    from screener.domain.commercial import UserSignalRecord

    writer.enqueue_record(UserSignalRecord(1, s.id, "sent", None, 1, T0, T0))
    assert await writer.flush_once() == 2
    assert calls == ["signals:1", "records:1"]
    assert writer.backlog == 0
