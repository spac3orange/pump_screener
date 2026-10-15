from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from aiogram import BaseMiddleware
from aiogram.filters import Filter
from aiogram.types import CallbackQuery, Message, TelegramObject, Update

from screener.app_state import AppState
from screener.domain.commercial import UserStatus
from screener.infrastructure.telegram.commercial_texts import RATE_LIMITED_TEXT

if TYPE_CHECKING:
    from screener.commercial_runtime import CommercialRuntime

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


class AdminFilter(Filter):
    """Admin-only routes; everyone else falls through to the subscriber router."""

    async def __call__(self, event: TelegramObject, state: AppState) -> bool:
        user = getattr(event, "from_user", None)
        return state.system.is_admin(getattr(user, "id", None))


def _is_payment_update(event: TelegramObject) -> bool:
    if not isinstance(event, Update):
        return False
    if event.pre_checkout_query is not None:
        return True
    msg = event.message
    return msg is not None and (
        msg.successful_payment is not None or msg.refunded_payment is not None
    )


class UserContextMiddleware(BaseMiddleware):
    """Commercial mode: register every private-chat user, attach ``account``.

    Replaces ``AdminAccessMiddleware``. Admin routes are still guarded by
    ``AdminFilter``; non-admins are rate limited and only reach subscriber routes.
    """

    def __init__(self, app_state: AppState, runtime: CommercialRuntime) -> None:
        self._app_state = app_state
        self._rt = runtime

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None or getattr(user, "is_bot", False):
            return None
        is_admin = self._app_state.system.is_admin(user.id)
        payment = _is_payment_update(event)
        data["state"] = self._app_state
        data["commercial"] = self._rt
        if not is_admin and not payment and not self._rt.rate_limiter.allow(user.id):
            if isinstance(event, Update) and event.callback_query is not None:
                with contextlib.suppress(Exception):
                    await event.callback_query.answer(RATE_LIMITED_TEXT)
            return None
        if isinstance(event, Update) and event.pre_checkout_query is not None:
            # 10 s answer deadline: skip registration, the payment service checks ownership.
            return await handler(event, data)
        chat = data.get("event_chat")
        if chat is not None and chat.type != "private":
            return None if not is_admin else await handler(event, data)
        try:
            account = await self._rt.users.register(
                telegram_user_id=user.id,
                chat_id=chat.id if chat is not None else user.id,
                language_code=user.language_code,
                is_admin=is_admin,
            )
        except Exception:
            logger.warning("User registration failed for an incoming update", exc_info=True)
            if is_admin or payment:
                # Admin tools and payment application must not depend on this upsert.
                return await handler(event, data)
            if isinstance(event, Update) and event.message is not None:
                with contextlib.suppress(Exception):
                    await event.message.answer("Сервис временно недоступен, попробуйте позже.")
            return None
        if account.status is UserStatus.BANNED and not is_admin:
            return None
        data["account"] = account
        return await handler(event, data)
