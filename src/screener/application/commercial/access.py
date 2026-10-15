"""Pure access rules. Every decision takes ``now`` explicitly; nothing reads a clock."""

from __future__ import annotations

from datetime import datetime, timedelta

from screener.domain.commercial import (
    AccessKind,
    AccessState,
    SubscriptionInfo,
    SubscriptionStatus,
    UserAccount,
    UserRole,
)


def subscription_access_end(
    subscription: SubscriptionInfo | None, *, grace: timedelta
) -> datetime | None:
    """Last instant (exclusive) a subscription grants access.

    Auto-renewing subscriptions keep a short grace so a renewal payment that
    lands a few minutes after the period boundary never looks like an expiry.
    Canceled subscriptions end exactly at the paid period end.
    """
    if subscription is None:
        return None
    if subscription.status is SubscriptionStatus.ACTIVE:
        if subscription.cancel_at_period_end:
            return subscription.current_period_end
        return subscription.current_period_end + grace
    if subscription.status is SubscriptionStatus.CANCELED:
        return subscription.current_period_end
    return None


def compute_access_until(
    *,
    trial_ends_at: datetime | None,
    subscription: SubscriptionInfo | None,
    grace: timedelta,
) -> datetime | None:
    ends = [
        t
        for t in (trial_ends_at, subscription_access_end(subscription, grace=grace))
        if t is not None
    ]
    return max(ends) if ends else None


def has_access(role: UserRole, access_until: datetime | None, now: datetime) -> bool:
    if role is UserRole.ADMIN:
        return True
    return access_until is not None and now < access_until


def access_state(
    user: UserAccount,
    subscription: SubscriptionInfo | None,
    now: datetime,
    *,
    grace: timedelta,
) -> AccessState:
    if user.role is UserRole.ADMIN:
        return AccessState(AccessKind.ADMIN)
    sub_end = subscription_access_end(subscription, grace=grace)
    if sub_end is not None and now < sub_end:
        assert subscription is not None
        return AccessState(AccessKind.PRO, subscription.current_period_end)
    if user.trial_ends_at is not None and now < user.trial_ends_at:
        return AccessState(AccessKind.TRIAL, user.trial_ends_at)
    return AccessState(AccessKind.NONE)


def trial_available(user: UserAccount, *, has_paid: bool) -> bool:
    return not user.trial_used and not has_paid and user.role is UserRole.USER
