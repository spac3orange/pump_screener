from __future__ import annotations

import asyncio
from types import SimpleNamespace

from screener.infrastructure.telegram.bot import TelegramBotService


async def test_stop_after_polling_died_does_not_raise() -> None:
    state = SimpleNamespace(system=SimpleNamespace(admin_id=1))
    service = TelegramBotService("123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", state)  # type: ignore[arg-type]

    async def polling_failed() -> None:
        raise ConnectionError("api.telegram.org unreachable")

    service._task = asyncio.create_task(polling_failed())
    await asyncio.sleep(0)
    assert service._task.done()

    await service.stop()  # must not block the persistence flush that follows
    assert service._task is None
