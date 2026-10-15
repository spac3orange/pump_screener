"""Offline aiogram helpers: a Bot API session that records calls, and update builders."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime

from aiogram.client.session.base import BaseSession
from aiogram.methods import CreateInvoiceLink, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

_ids = itertools.count(1)


class MockSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list = []

    async def make_request(self, bot, method, timeout=None):  # type: ignore[override]
        self.calls.append(method)
        if isinstance(method, SendMessage):
            return Message(
                message_id=len(self.calls),
                date=datetime.now(UTC),
                chat=Chat(id=int(method.chat_id), type="private"),
                text=method.text,
            )
        if isinstance(method, CreateInvoiceLink):
            return f"https://t.me/$invoice{len(self.calls)}"
        return True

    async def stream_content(self, *args, **kwargs):  # type: ignore[override]
        yield b""

    async def close(self) -> None:
        return None

    def texts(self, chat_id: int | None = None) -> list[str]:
        return [
            c.text
            for c in self.calls
            if isinstance(c, (SendMessage, EditMessageText))
            and (chat_id is None or int(c.chat_id or 0) == chat_id)
        ]

    def of(self, kind: type) -> list:
        return [c for c in self.calls if isinstance(c, kind)]


def next_id() -> int:
    return next(_ids)


def tg_user(uid: int) -> User:
    return User(id=uid, is_bot=False, first_name=f"u{uid}", language_code="ru")


def msg_update(uid: int, text: str | None = None, **extra) -> Update:
    return Update(
        update_id=next_id(),
        message=Message(
            message_id=next_id(),
            date=datetime.now(UTC),
            chat=Chat(id=uid, type="private"),
            from_user=tg_user(uid),
            text=text,
            **extra,
        ),
    )


def callback_update(uid: int, data: str) -> Update:
    return Update(
        update_id=next_id(),
        callback_query=CallbackQuery(
            id=str(next_id()),
            from_user=tg_user(uid),
            chat_instance="ci",
            data=data,
            message=Message(
                message_id=next_id(),
                date=datetime.now(UTC),
                chat=Chat(id=uid, type="private"),
                from_user=User(id=999, is_bot=True, first_name="bot"),
                text="old",
            ),
        ),
    )


def detach_routers() -> None:
    """Module-level routers can only have one parent; tests build several bots."""
    from screener.infrastructure.telegram.handlers import router
    from screener.infrastructure.telegram.payment_handlers import payment_router
    from screener.infrastructure.telegram.user_handlers import user_router

    for r in (router, user_router, payment_router):
        if r._parent_router is not None:
            r._parent_router.sub_routers.remove(r)
            r._parent_router = None
