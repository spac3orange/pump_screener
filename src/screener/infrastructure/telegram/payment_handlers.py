"""Telegram Stars updates. Registered first and exempt from the user rate limit."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.types import Message, PreCheckoutQuery

from screener.domain.commercial import PaymentInput
from screener.infrastructure.telegram import commercial_texts as texts

if TYPE_CHECKING:
    from screener.commercial_runtime import CommercialRuntime

logger = logging.getLogger(__name__)

payment_router = Router(name="commercial-payments")


def _as_utc(value: datetime | int | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, int):
        return datetime.fromtimestamp(value, tz=UTC)
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@payment_router.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery, commercial: CommercialRuntime) -> None:
    reason = await commercial.payments.pre_checkout(
        telegram_user_id=query.from_user.id,
        payload=query.invoice_payload,
        currency=query.currency,
        total_amount=query.total_amount,
    )
    if reason is None:
        await query.answer(ok=True)
    else:
        await query.answer(ok=False, error_message=reason)


@payment_router.message(F.successful_payment)
async def on_successful_payment(message: Message, commercial: CommercialRuntime) -> None:
    sp = message.successful_payment
    if sp is None or message.from_user is None:
        return
    payment = PaymentInput(
        telegram_user_id=message.from_user.id,
        currency=sp.currency,
        total_amount=sp.total_amount,
        invoice_payload=sp.invoice_payload,
        telegram_payment_charge_id=sp.telegram_payment_charge_id,
        is_recurring=bool(sp.is_recurring),
        is_first_recurring=bool(sp.is_first_recurring),
        subscription_expiration_date=_as_utc(sp.subscription_expiration_date),
    )
    outcome = await commercial.payments.successful_payment(payment)
    if outcome.duplicate:
        return
    if outcome.applied:
        await message.answer(
            texts.payment_applied_text(outcome.period_end, renewal=outcome.renewal)
        )
    else:
        await message.answer(texts.PAYMENT_PROBLEM_TEXT)


@payment_router.message(F.refunded_payment)
async def on_refunded_payment(message: Message, commercial: CommercialRuntime) -> None:
    rp = message.refunded_payment
    if rp is None or message.from_user is None:
        return
    changed = await commercial.payments.refunded(
        telegram_user_id=message.from_user.id,
        charge_id=rp.telegram_payment_charge_id,
        currency=rp.currency,
        total_amount=rp.total_amount,
        invoice_payload=rp.invoice_payload or "",
    )
    if changed:
        await message.answer(texts.REFUNDED_TEXT)
