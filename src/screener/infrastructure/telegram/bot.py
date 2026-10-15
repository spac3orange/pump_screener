from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import LabeledPrice

from screener.application.commercial.dispatcher import DeliveryBlocked, DeliveryRetryAfter
from screener.domain.commercial import STARS_CURRENCY, UserAccount
from screener.domain.models import MarketSignal
from screener.domain.trade_lifecycle import TradeClosedEvent
from screener.infrastructure.telegram.commercial_texts import access_ended_text
from screener.infrastructure.telegram.formatter import format_signal_message
from screener.infrastructure.telegram.handlers import router
from screener.infrastructure.telegram.middleware import (
    AdminAccessMiddleware,
    AppStateMiddleware,
    UserContextMiddleware,
)
from screener.infrastructure.telegram.payment_handlers import payment_router
from screener.infrastructure.telegram.trade_lifecycle_format import format_trade_closed
from screener.infrastructure.telegram.user_handlers import user_router

if TYPE_CHECKING:
    from screener.app_state import AppState

logger = logging.getLogger(__name__)

_RETRY_AFTER_ATTEMPTS = 3


class StarsBotGateway:
    """Telegram Stars Bot API calls used by ``PaymentService``."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def create_subscription_link(
        self, *, title: str, description: str, payload: str, amount_stars: int, period_sec: int
    ) -> str:
        return await self._bot.create_invoice_link(
            title=title,
            description=description,
            payload=payload,
            currency=STARS_CURRENCY,
            prices=[LabeledPrice(label=title, amount=amount_stars)],
            subscription_period=period_sec,
        )

    async def set_subscription_canceled(
        self, telegram_user_id: int, charge_id: str, canceled: bool
    ) -> None:
        await self._bot.edit_user_star_subscription(
            user_id=telegram_user_id,
            telegram_payment_charge_id=charge_id,
            is_canceled=canceled,
        )

    async def refund(self, telegram_user_id: int, charge_id: str) -> None:
        await self._bot.refund_star_payment(
            user_id=telegram_user_id, telegram_payment_charge_id=charge_id
        )


class TelegramBotService:
    def __init__(self, token: str, app_state: AppState) -> None:
        self._bot = Bot(
            token=token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self._dp = Dispatcher()
        self._dp.update.middleware(AppStateMiddleware(app_state))
        commercial = getattr(app_state, "commercial", None)
        if commercial is None:
            # Admin-only bot: enforce the ADMIN_ID allowlist for all updates.
            self._dp.update.middleware(AdminAccessMiddleware(app_state))
            self._dp.include_router(router)
        else:
            # Payments first (10 s pre-checkout deadline), then admin tools, then subscribers.
            self._dp.update.middleware(UserContextMiddleware(app_state, commercial))
            self._dp.include_routers(payment_router, router, user_router)
        self._app_state = app_state
        self._task = None
        self.stars = StarsBotGateway(self._bot)
        logger.info(
            "Telegram bot configured for admin_id=%s commercial=%s",
            app_state.system.admin_id,
            commercial is not None,
        )

    @property
    def bot(self) -> Bot:
        return self._bot

    async def send_to_chat(self, chat_id: int, text: str) -> None:
        """Single attempt for subscriber delivery; the dispatcher owns retries and pacing."""
        try:
            await self._bot.send_message(chat_id, text)
        except TelegramRetryAfter as exc:
            raise DeliveryRetryAfter(float(exc.retry_after)) from None
        except TelegramForbiddenError:
            raise DeliveryBlocked() from None
        except TelegramBadRequest as exc:
            if "chat not found" in str(exc).lower() or "user is deactivated" in str(exc).lower():
                raise DeliveryBlocked() from None
            raise

    async def notify_access_ended(self, user: UserAccount) -> None:
        commercial = self._app_state.commercial
        if commercial is None:
            return
        await self.send_to_chat(user.chat_id, access_ended_text(user, commercial.settings))

    async def send_signal(self, chat_id: int, signal: MarketSignal, signals_24h: int = 1) -> None:
        text = format_signal_message(signal, signals_24h=signals_24h)
        await self._send_with_retry(chat_id, text)

    async def broadcast_text(self, text: str) -> None:
        for chat_id in self._app_state.system.admin_id:
            try:
                await self._send_with_retry(chat_id, text)
            except Exception:
                logger.exception("Failed to broadcast trading message to %s", chat_id)

    async def deliver_text(self, text: str) -> bool:
        """Like ``broadcast_text`` but reports whether at least one admin got it."""
        delivered = False
        for chat_id in self._app_state.system.admin_id:
            try:
                await self._send_with_retry(chat_id, text)
                delivered = True
            except Exception:
                logger.exception("Failed to deliver trading message to %s", chat_id)
        return delivered

    async def notify_trade_closed(self, event: TradeClosedEvent) -> bool:
        return await self.deliver_text(format_trade_closed(event))

    async def _send_with_retry(self, chat_id: int, text: str) -> None:
        for attempt in range(_RETRY_AFTER_ATTEMPTS + 1):
            try:
                await self._bot.send_message(chat_id, text)
                return
            except TelegramRetryAfter as exc:
                if attempt == _RETRY_AFTER_ATTEMPTS:
                    raise
                logger.warning("Telegram flood control: retry after %ss", exc.retry_after)
                await asyncio.sleep(exc.retry_after)

    async def start_polling(self) -> None:
        self._task = asyncio.create_task(
            self._dp.start_polling(self._bot, handle_signals=False),
            name="telegram-polling",
        )

    async def stop(self) -> None:
        if self._task:
            # Raises "Polling is not started" if polling died (e.g. Telegram unreachable);
            # shutdown must still reach the persistence flush.
            with contextlib.suppress(RuntimeError):
                await self._dp.stop_polling()
            self._task.cancel()
            try:
                await self._task
            except Exception:
                pass
            self._task = None
        await self._bot.session.close()
