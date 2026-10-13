import asyncio

import pytest

from screener.application.notification_service import NotificationService
from screener.application.persistence_service import PersistenceService
from screener.domain.enums import DeliveryStatus, SignalType
from tests.helpers import make_signal


class _SlowDb:
    """Database that hangs on every call."""

    def __init__(self) -> None:
        self.saved: list = []
        self.deliveries: list = []

    async def count_symbol_signals_24h(self, symbol, *, now, exclude_id=None) -> int:
        await asyncio.sleep(10)
        return 0

    async def save_signal(self, signal) -> None:
        await asyncio.sleep(10)

    async def save_notification(self, signal_id, chat_id, status, error=None) -> None:
        await asyncio.sleep(10)


class _GoodDb:
    def __init__(self) -> None:
        self.order: list[str] = []

    async def count_symbol_signals_24h(self, symbol, *, now, exclude_id=None) -> int:
        return 2

    async def save_signal(self, signal) -> None:
        self.order.append(f"signal:{signal.id}")

    async def save_notification(self, signal_id, chat_id, status, error=None) -> None:
        self.order.append(f"delivery:{signal_id}:{status.value}")


@pytest.mark.asyncio
async def test_alert_is_sent_when_database_hangs() -> None:
    db = _SlowDb()
    persistence = PersistenceService(db, op_timeout_sec=5)
    sent: list[tuple[int, int]] = []

    async def send(chat_id, signal, signals_24h=1):
        sent.append((chat_id, signals_24h))

    notifications = NotificationService(
        chat_ids=[1],
        counter=db,
        persistence=persistence,
        send_func=send,
        count_timeout_sec=0.05,
    )
    await persistence.start()
    await notifications.start()
    signal = make_signal(SignalType.PUMP, 720, 41.0)
    persistence.enqueue_signal(signal)
    notifications.enqueue(signal)
    await asyncio.sleep(0.3)
    assert sent == [(1, 1)]
    assert persistence.backlog >= 1
    await notifications.stop()
    await persistence.stop()


@pytest.mark.asyncio
async def test_delivery_record_written_after_signal_row() -> None:
    db = _GoodDb()
    persistence = PersistenceService(db)

    async def send(chat_id, signal, signals_24h=1):
        assert signals_24h == 3

    notifications = NotificationService(
        chat_ids=[7], counter=db, persistence=persistence, send_func=send
    )
    await persistence.start()
    await notifications.start()
    signal = make_signal(SignalType.DUMP, 900, -45.0)
    persistence.enqueue_signal(signal)
    notifications.enqueue(signal)
    await asyncio.sleep(0.2)
    assert db.order == [
        f"signal:{signal.id}",
        f"delivery:{signal.id}:{DeliveryStatus.SENT.value}",
    ]
    await notifications.stop()
    await persistence.stop()
