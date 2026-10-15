"""Subscription state machine: pure decisions applied inside one DB transaction.

Telegram is authoritative only through ``successful_payment``; pre-checkout
never grants access. Renewals arrive as new ``successful_payment`` messages
that reuse the original invoice payload with a new charge id.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from screener.domain.commercial import (
    STARS_CURRENCY,
    SUBSCRIPTION_PERIOD_SEC,
    InvoiceInfo,
    PaymentInput,
    PaymentKind,
    SubscriptionInfo,
    SubscriptionStatus,
    UserAccount,
)

PERIOD = timedelta(seconds=SUBSCRIPTION_PERIOD_SEC)


class PaymentAction(StrEnum):
    CREATE = "create"
    EXTEND = "extend"
    NONE = "none"


@dataclass(slots=True, frozen=True)
class PaymentDecision:
    action: PaymentAction
    anomaly: str | None = None
    period_end: datetime | None = None
    subscription_id: UUID | None = None

    @property
    def applied(self) -> bool:
        return self.anomaly is None


def _anomaly(reason: str) -> PaymentDecision:
    return PaymentDecision(PaymentAction.NONE, anomaly=reason)


def decide_payment(
    payment: PaymentInput,
    *,
    owner: UserAccount | None,
    invoice: InvoiceInfo | None,
    subscription_for_payload: SubscriptionInfo | None,
    live_subscription: SubscriptionInfo | None,
    now: datetime,
) -> PaymentDecision:
    if payment.currency != STARS_CURRENCY:
        return _anomaly("currency")
    if owner is None:
        return _anomaly("unknown_user")
    if payment.kind is PaymentKind.INITIAL:
        if invoice is None:
            return _anomaly("unknown_payload")
        if invoice.user_id != owner.id:
            return _anomaly("owner_mismatch")
        if payment.total_amount != invoice.amount_stars:
            return _anomaly("amount_mismatch")
        if subscription_for_payload is not None:
            return _anomaly("payload_reused")
        if live_subscription is not None:
            return _anomaly("duplicate_subscription")
        end = payment.subscription_expiration_date or now + PERIOD
        return PaymentDecision(PaymentAction.CREATE, period_end=end)

    sub = subscription_for_payload
    if sub is None:
        return _anomaly("unknown_subscription")
    if sub.user_id != owner.id:
        return _anomaly("owner_mismatch")
    if invoice is not None and payment.total_amount != invoice.amount_stars:
        return _anomaly("amount_mismatch")
    if sub.status is SubscriptionStatus.REFUNDED:
        return _anomaly("renewal_after_refund")
    if live_subscription is not None and live_subscription.id != sub.id:
        return _anomaly("duplicate_subscription")
    proposed = payment.subscription_expiration_date or sub.current_period_end + PERIOD
    return PaymentDecision(
        PaymentAction.EXTEND,
        period_end=max(sub.current_period_end, proposed),
        subscription_id=sub.id,
    )


def expired_by(sub: SubscriptionInfo, now: datetime, *, grace: timedelta) -> bool:
    """Mirror of the SQL in ``CommercialRepository.expire_subscriptions``."""
    if not sub.is_live:
        return False
    if sub.status is SubscriptionStatus.CANCELED or sub.cancel_at_period_end:
        return now >= sub.current_period_end
    return now >= sub.current_period_end + grace
