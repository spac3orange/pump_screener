from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from screener.domain.models import SignalEvent
from screener.infrastructure.telegram.formatter import format_signal_message
from screener.infrastructure.telegram.handlers import router
from screener.infrastructure.telegram.middleware import AdminAccessMiddleware, AppStateMiddleware

if TYPE_CHECKING:
    from screener.app_state import AppState

logger = logging.getLogger(__name__)


class TelegramBotService:
    def __init__(self, token: str, app_state: AppState) -> None:
        self._bot = Bot(
            token=token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self._dp = Dispatcher()
        # Outer: inject state; then enforce ADMIN_ID allowlist for all updates.
        self._dp.update.middleware(AppStateMiddleware(app_state))
        self._dp.update.middleware(AdminAccessMiddleware(app_state))
        self._dp.include_router(router)
        self._app_state = app_state
        self._task = None
        logger.info(
            "Telegram bot configured for admin_id=%s",
            app_state.system.admin_id,
        )

    @property
    def bot(self) -> Bot:
        return self._bot

    async def send_signal(
        self, chat_id: int, signal: SignalEvent, signals_24h: int = 1
    ) -> None:
        text = format_signal_message(signal, signals_24h=signals_24h)
        await self._bot.send_message(chat_id, text)

    async def start_polling(self) -> None:
        import asyncio

        self._task = asyncio.create_task(
            self._dp.start_polling(self._bot, handle_signals=False),
            name="telegram-polling",
        )

    async def stop(self) -> None:
        if self._task:
            await self._dp.stop_polling()
            self._task.cancel()
            try:
                await self._task
            except Exception:
                pass
            self._task = None
        await self._bot.session.close()
