from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, Update

from screener.app_state import AppState

logger = logging.getLogger(__name__)


def _command_user_id(event: TelegramObject) -> int | None:
    """User id when the incoming update is a bot command, otherwise None.

    Dispatcher update middleware receives ``Update``, not ``Message``.
    """
    message = event.message if isinstance(event, Update) else event
    if not isinstance(message, Message):
        return None
    text = message.text or ""
    if not text.startswith("/") or message.from_user is None:
        return None
    return message.from_user.id


class AppStateMiddleware(BaseMiddleware):
    def __init__(self, app_state: AppState) -> None:
        self._app_state = app_state

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user_id = _command_user_id(event)
        if user_id is not None:
            self._app_state.clear_pending_edit(user_id)
        data["state"] = self._app_state
        return await handler(event, data)


class AdminAccessMiddleware(BaseMiddleware):
    """Allow only Telegram user IDs listed in ADMIN_ID."""

    def __init__(self, app_state: AppState) -> None:
        self._app_state = app_state

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        user_id = getattr(user, "id", None)
        if not self._app_state.system.is_admin(user_id):
            logger.warning("Rejected non-admin telegram user_id=%s", user_id)
            if isinstance(event, Message):
                await event.answer("Доступ запрещён. Бот доступен только администраторам.")
            elif isinstance(event, CallbackQuery):
                await event.answer("Доступ запрещён", show_alert=True)
            return None
        data["state"] = self._app_state
        return await handler(event, data)
