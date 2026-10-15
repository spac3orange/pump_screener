"""COMMERCIAL_ENABLED=false keeps the bot admin-only, exactly as before."""

from __future__ import annotations

from types import SimpleNamespace

from screener.infrastructure.telegram.bot import TelegramBotService
from tests.telegram_fakes import MockSession, detach_routers, msg_update

ADMIN = 1
STRANGER = 2002


async def test_non_admin_is_rejected_when_commercial_disabled() -> None:
    state = SimpleNamespace(
        system=SimpleNamespace(admin_id=[ADMIN], is_admin=lambda uid: uid == ADMIN),
        commercial=None,
        clear_pending_edit=lambda uid: None,
    )
    detach_routers()
    svc = TelegramBotService("123456:TEST-token", state)  # type: ignore[arg-type]
    session = MockSession()
    svc.bot.session = session
    try:
        for text in ("/start", "/account", "/subscription", "/settings", "hello"):
            await svc._dp.feed_update(svc.bot, msg_update(STRANGER, text))
        await svc._dp.feed_update(svc.bot, msg_update(ADMIN, "/start"))
    finally:
        detach_routers()
    assert session.texts(STRANGER) == []
    assert len(session.texts(ADMIN)) == 1
