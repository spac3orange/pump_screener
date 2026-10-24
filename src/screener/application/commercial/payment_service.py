"""Telegram Stars subscription flow: invoice link, pre-checkout, payment, renewal, refund.

Access is granted only from ``successful_payment`` (applied idempotently in one
transaction). Nothing here touches trading; payments only change ``access_until``.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from screener.application.clock import Clock
from screener.application.commercial.user_service import UserService
from screener.application.metrics import metrics
from screener.domain.commercial import (
    PLAN_PRO_30D,
    STARS_CURRENCY,
    SUBSCRIPTION_PERIOD_SEC,
    InvoiceStatus,
    PaymentInput,
    SubscriptionInfo,
    UserAccount,
    UserStatus,
)

logger = logging.getLogger(__name__)

PAYLOAD_PREFIX = "ps1_"
PRE_CHECKOUT_TIMEOUT_SEC = 3.0
APPLY_ATTEMPTS = 5
APPLY_RETRY_BASE_SEC = 1.0


class StarsGateway(Protocol):
    async def create_subscription_link(
        self, *, title: str, description: str, payload: str, amount_stars: int, period_sec: int
    ) -> str: ...

    async def set_subscription_canceled(
        self, telegram_user_id: int, charge_id: str, canceled: bool
    ) -> None: ...

    async def refund(self, telegram_user_id: int, charge_id: str) -> None: ...


AlertFunc = Callable[[str], Awaitable[Any]]


class InvoiceError(StrEnum):
    ADMIN = "admin"
    TERMS_REQUIRED = "terms_required"
    ALREADY_SUBSCRIBED = "already_subscribed"
    RATE_LIMITED = "rate_limited"
    GATEWAY = "gateway"


@dataclass(slots=True, frozen=True)
class InvoiceResult:
    link: str | None = None
    error: InvoiceError | None = None


@dataclass(slots=True, frozen=True)
class PaymentOutcome:
    applied: bool
    duplicate: bool = False
    anomaly: str | None = None
    renewal: bool = False
    user: UserAccount | None = None
    period_end: datetime | None = None


class SubscriptionChangeError(StrEnum):
    NO_SUBSCRIPTION = "no_subscription"
    ALREADY = "already"
    GATEWAY = "gateway"


def new_payload() -> str:
    """Opaque, unguessable, server-generated invoice payload (≤ 64 bytes)."""
    return (PAYLOAD_PREFIX + secrets.token_urlsafe(32))[:64]


class PaymentService:
    def __init__(
        self,
        store,
        users: UserService,
        gateway: StarsGateway | None,
        clock: Clock,
        *,
        alert_admins: AlertFunc | None = None,
    ) -> None:
        self._store = store
        self._users = users
        self._gateway = gateway
        self._clock = clock
        self._alert = alert_admins

    def set_gateway(self, gateway: StarsGateway) -> None:
        self._gateway = gateway

    def set_alert(self, alert: AlertFunc) -> None:
        self._alert = alert

    @property
    def price_stars(self) -> int:
        return self._users.settings.subscription_price_stars

    # --- invoice -----------------------------------------------------------------

    async def create_invoice(self, user: UserAccount) -> InvoiceResult:
        settings = self._users.settings
        if user.is_admin:
            return InvoiceResult(error=InvoiceError.ADMIN)
        if not user.terms_accepted(settings.terms_version):
            return InvoiceResult(error=InvoiceError.TERMS_REQUIRED)
        if await self._store.get_live_subscription(user.id) is not None:
            return InvoiceResult(error=InvoiceError.ALREADY_SUBSCRIBED)
        now = self._clock.now()
        recent = await self._store.count_invoices_since(user.id, now - timedelta(hours=1))
        if recent >= settings.invoice_rate_limit_per_hour:
            return InvoiceResult(error=InvoiceError.RATE_LIMITED)
        if self._gateway is None:
            return InvoiceResult(error=InvoiceError.GATEWAY)
        payload = new_payload()
        await self._store.create_invoice(
            payload=payload,
            user_id=user.id,
            plan_code=PLAN_PRO_30D,
            amount_stars=settings.subscription_price_stars,
            currency=STARS_CURRENCY,
            now=now,
            expires_at=now + timedelta(hours=settings.invoice_ttl_hours),
        )
        try:
            link = await self._gateway.create_subscription_link(
                title=settings.subscription_title,
                description=(
                    "Сигналы PumpScreener по вашим правилам на 30 дней. "
                    "Подписка продлевается автоматически, отменить можно в любой момент."
                ),
                payload=payload,
                amount_stars=settings.subscription_price_stars,
                period_sec=SUBSCRIPTION_PERIOD_SEC,
            )
        except Exception as exc:
            logger.warning("createInvoiceLink failed: %s", type(exc).__name__)
            return InvoiceResult(error=InvoiceError.GATEWAY)
        logger.info("Invoice link created user_id=%s", user.id)
        return InvoiceResult(link=link)

    # --- pre-checkout ------------------------------------------------------------

    async def pre_checkout(
        self, *, telegram_user_id: int, payload: str, currency: str, total_amount: int
    ) -> str | None:
        """``None`` to accept, otherwise a user-facing reason. Never grants access."""
        try:
            reason = await asyncio.wait_for(
                self._check_pre_checkout(telegram_user_id, payload, currency, total_amount),
                timeout=PRE_CHECKOUT_TIMEOUT_SEC,
            )
        except Exception as exc:
            logger.warning("pre_checkout validation failed: %s", type(exc).__name__)
            reason = "Сервис временно недоступен. Попробуйте позже."
        if reason is not None:
            metrics.pre_checkout_rejected_total += 1
        return reason

    async def _check_pre_checkout(
        self, telegram_user_id: int, payload: str, currency: str, total_amount: int
    ) -> str | None:
        invoice = await self._store.get_invoice(payload)
        if invoice is None:
            return "Счёт не найден. Откройте /subscription и создайте новый."
        user = await self._store.get_user_by_telegram(telegram_user_id)
        if user is None or user.id != invoice.user_id:
            return "Этот счёт выставлен другому пользователю."
        if user.status is UserStatus.BANNED:
            return "Оплата недоступна для этого аккаунта."
        if currency != STARS_CURRENCY or total_amount != invoice.amount_stars:
            return "Сумма счёта не совпадает. Создайте новый счёт."
        if invoice.status not in (InvoiceStatus.CREATED, InvoiceStatus.PRE_CHECKOUT_OK):
            return "Счёт уже использован. Создайте новый в /subscription."
        if self._clock.now() >= invoice.expires_at:
            return "Срок действия счёта истёк. Создайте новый в /subscription."
        if await self._store.get_live_subscription(user.id) is not None:
            return "Подписка уже активна."
        await self._store.mark_invoice_pre_checkout(payload)
        return None

    # --- successful payment ------------------------------------------------------

    async def successful_payment(self, payment: PaymentInput) -> PaymentOutcome:
        # Telegram does not redeliver a consumed update, so retry here (idempotent apply).
        for attempt in range(APPLY_ATTEMPTS):
            try:
                result = await self._store.apply_successful_payment(
                    payment, now=self._clock.now(), grace=self._users.grace
                )
                break
            except Exception as exc:
                logger.warning(
                    "Applying payment failed (attempt %s/%s): %s",
                    attempt + 1,
                    APPLY_ATTEMPTS,
                    type(exc).__name__,
                )
                if attempt + 1 == APPLY_ATTEMPTS:
                    await self._notify_admins(
                        "🚨 <b>Payment NOT applied</b> (DB error)\n"
                        f"tg_user: <code>{payment.telegram_user_id}</code>\n"
                        f"charge: <code>{payment.telegram_payment_charge_id}</code>\n"
                        "Запустите scripts/reconcile_stars.py после восстановления БД."
                    )
                    raise
                await asyncio.sleep(APPLY_RETRY_BASE_SEC * 2**attempt)
        renewal = payment.is_recurring and not payment.is_first_recurring
        if result.duplicate and attempt > 0:
            # Our earlier attempt committed but reported an error: finish it, don't hide it.
            user = await self._store.get_user_by_telegram(payment.telegram_user_id)
            if user is not None:
                refreshed = await self._users.refresh(user.id) or user
                logger.info("Payment applied on an earlier attempt user_id=%s", user.id)
                return PaymentOutcome(
                    applied=True,
                    renewal=renewal,
                    user=refreshed,
                    period_end=refreshed.access_until,
                )
        if result.duplicate:
            metrics.payments_duplicate_total += 1
            logger.info("Duplicate successful_payment ignored")
            return PaymentOutcome(applied=False, duplicate=True, renewal=renewal)
        decision = result.decision
        assert decision is not None
        if not decision.applied:
            metrics.payments_anomaly_total += 1
            logger.warning(
                "Payment anomaly reason=%s tg_user=%s amount=%s",
                decision.anomaly,
                payment.telegram_user_id,
                payment.total_amount,
            )
            if decision.anomaly == "duplicate_subscription" and self._gateway is not None:
                # A second live subscription must not keep billing the user.
                try:
                    await self._gateway.set_subscription_canceled(
                        payment.telegram_user_id, payment.telegram_payment_charge_id, True
                    )
                except Exception as exc:
                    logger.warning(
                        "Cancel of duplicate subscription failed: %s", type(exc).__name__
                    )
            await self._notify_admins(
                "⚠️ <b>Payment anomaly</b>\n"
                f"reason: <code>{decision.anomaly}</code>\n"
                f"tg_user: <code>{payment.telegram_user_id}</code>\n"
                f"amount: {payment.total_amount} {payment.currency}\n"
                f"charge: <code>{payment.telegram_payment_charge_id}</code>\n"
                "Доступ не выдан. /admin_refund такой платёж не найдёт: верните Stars через "
                "Bot API refundStarPayment (см. docs/COMMERCIAL_RUNBOOK.md, раздел 4)."
            )
            return PaymentOutcome(
                applied=False, anomaly=decision.anomaly, renewal=renewal, user=result.user
            )
        metrics.payments_applied_total += 1
        assert result.user is not None
        logger.info(
            "Payment applied user_id=%s kind=%s period_end=%s",
            result.user.id,
            payment.kind.value,
            decision.period_end,
        )
        await self._users.apply_account(result.user)
        return PaymentOutcome(
            applied=True, renewal=renewal, user=result.user, period_end=decision.period_end
        )

    # --- auto-renew --------------------------------------------------------------

    async def cancel_autorenew(
        self, user: UserAccount
    ) -> tuple[SubscriptionInfo | None, SubscriptionChangeError | None]:
        return await self._set_canceled(user, True)

    async def resume_autorenew(
        self, user: UserAccount
    ) -> tuple[SubscriptionInfo | None, SubscriptionChangeError | None]:
        return await self._set_canceled(user, False)

    async def _set_canceled(
        self, user: UserAccount, cancel: bool
    ) -> tuple[SubscriptionInfo | None, SubscriptionChangeError | None]:
        sub = await self._store.get_live_subscription(user.id)
        if sub is None or (not cancel and self._clock.now() >= sub.current_period_end):
            return None, SubscriptionChangeError.NO_SUBSCRIPTION
        if sub.cancel_at_period_end == cancel:
            return sub, SubscriptionChangeError.ALREADY
        if self._gateway is None:
            return sub, SubscriptionChangeError.GATEWAY
        try:
            # Telegram bills the user; change it there first, then mirror locally.
            await self._gateway.set_subscription_canceled(
                user.telegram_user_id, sub.subscription_charge_id, cancel
            )
        except Exception as exc:
            logger.warning("editUserStarSubscription failed: %s", type(exc).__name__)
            return sub, SubscriptionChangeError.GATEWAY
        changed = await self._store.set_cancel_at_period_end(
            user.id, cancel=cancel, now=self._clock.now(), grace=self._users.grace
        )
        if changed is None:
            return None, SubscriptionChangeError.NO_SUBSCRIPTION
        new_sub, account = changed
        await self._users.apply_account(account)
        logger.info("Subscription auto-renew %s user_id=%s", "off" if cancel else "on", user.id)
        return new_sub, None

    # --- refunds -----------------------------------------------------------------

    async def refunded(
        self,
        *,
        telegram_user_id: int,
        charge_id: str,
        currency: str,
        total_amount: int,
        invoice_payload: str,
    ) -> bool:
        """Apply a ``refunded_payment`` update; returns whether it changed anything."""
        result = await self._store.apply_refund(
            charge_id=charge_id,
            telegram_user_id=telegram_user_id,
            currency=currency,
            total_amount=total_amount,
            invoice_payload=invoice_payload,
            now=self._clock.now(),
            grace=self._users.grace,
        )
        if not result.found:
            logger.warning("Refund for unknown payment tg_user=%s", telegram_user_id)
            return False
        if result.already_refunded:
            return False
        metrics.refunds_total += 1
        if result.user is not None:
            await self._users.apply_account(result.user)
        if result.subscription is not None and self._gateway is not None:
            # A refunded subscription must not renew; best effort, access is already revoked.
            try:
                await self._gateway.set_subscription_canceled(
                    telegram_user_id, result.subscription.subscription_charge_id, True
                )
            except Exception as exc:
                logger.info("Post-refund auto-renew cancel skipped: %s", type(exc).__name__)
        logger.info("Refund applied user_id=%s", result.user.id if result.user else None)
        return True

    async def admin_refund(self, charge_id: str) -> str:
        ref = await self._store.find_payment(charge_id)
        if ref is None:
            return "Платёж с таким charge id не найден."
        if self._gateway is None:
            return "Telegram недоступен."
        try:
            await self._gateway.refund(ref.telegram_user_id, charge_id)
        except Exception as exc:
            logger.warning("refundStarPayment failed: %s", type(exc).__name__)
            return f"Telegram отклонил возврат: {type(exc).__name__}"
        changed = await self.refunded(
            telegram_user_id=ref.telegram_user_id,
            charge_id=charge_id,
            currency=ref.currency,
            total_amount=ref.total_amount,
            invoice_payload=ref.invoice_payload,
        )
        return "Возврат выполнен, доступ отозван." if changed else "Возврат уже был учтён."

    async def _notify_admins(self, text: str) -> None:
        if self._alert is None:
            return
        try:
            await self._alert(text)
        except Exception:
            logger.warning("Failed to alert admins about a payment anomaly", exc_info=True)
