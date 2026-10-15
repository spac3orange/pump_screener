from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from screener.application.clock import FakeClock
from screener.application.commercial.access import (
    access_state,
    compute_access_until,
    has_access,
    subscription_access_end,
    trial_available,
)
from screener.config import ScreenerSettings
from screener.domain.commercial import (
    AccessKind,
    SubscriptionInfo,
    SubscriptionStatus,
    UserAccount,
    UserDetectionConfig,
    UserRole,
    UserStatus,
)

T = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
GRACE = timedelta(minutes=15)


def user(**kw) -> UserAccount:
    base = dict(
        id=1,
        telegram_user_id=100,
        chat_id=100,
        role=UserRole.USER,
        status=UserStatus.ACTIVE,
        monitoring_enabled=True,
        terms_version=None,
        terms_accepted_at=None,
        trial_started_at=None,
        trial_ends_at=None,
        access_until=None,
        expiry_notified_until=None,
    )
    base.update(kw)
    return UserAccount(**base)


def sub(status=SubscriptionStatus.ACTIVE, *, end=T + timedelta(days=30), cancel=False):
    return SubscriptionInfo(
        id=uuid4(),
        user_id=1,
        plan_code="pro_30d",
        status=status,
        invoice_payload="ps1_x",
        subscription_charge_id="ch1",
        current_period_end=end,
        cancel_at_period_end=cancel,
    )


def test_fake_clock_now_is_utc() -> None:
    clock = FakeClock(int(T.timestamp() * 1000))
    assert clock.now() == T
    clock.advance(1000)
    assert clock.now() == T + timedelta(seconds=1)


def test_no_access_by_default() -> None:
    assert access_state(user(), None, T, grace=GRACE).kind is AccessKind.NONE
    assert not has_access(UserRole.USER, None, T)


def test_trial_boundary_is_exclusive() -> None:
    u = user(trial_started_at=T, trial_ends_at=T + timedelta(hours=24))
    assert access_state(u, None, T, grace=GRACE).kind is AccessKind.TRIAL
    end = T + timedelta(hours=24)
    assert access_state(u, None, end - timedelta(microseconds=1), grace=GRACE).active
    assert access_state(u, None, end, grace=GRACE).kind is AccessKind.NONE
    assert has_access(UserRole.USER, end, end - timedelta(seconds=1))
    assert not has_access(UserRole.USER, end, end)


def test_active_subscription_has_grace_canceled_does_not() -> None:
    end = T + timedelta(days=30)
    assert subscription_access_end(sub(end=end), grace=GRACE) == end + GRACE
    assert subscription_access_end(sub(end=end, cancel=True), grace=GRACE) == end
    assert subscription_access_end(sub(SubscriptionStatus.CANCELED, end=end), grace=GRACE) == end
    assert subscription_access_end(sub(SubscriptionStatus.EXPIRED), grace=GRACE) is None
    assert subscription_access_end(sub(SubscriptionStatus.REFUNDED), grace=GRACE) is None


def test_pro_wins_over_trial_and_access_until_is_max() -> None:
    u = user(trial_started_at=T, trial_ends_at=T + timedelta(hours=24))
    s = sub()
    state = access_state(u, s, T, grace=GRACE)
    assert state.kind is AccessKind.PRO and state.until == s.current_period_end
    until = compute_access_until(trial_ends_at=u.trial_ends_at, subscription=s, grace=GRACE)
    assert until == s.current_period_end + GRACE
    assert (
        compute_access_until(trial_ends_at=u.trial_ends_at, subscription=None, grace=GRACE)
        == u.trial_ends_at
    )
    assert compute_access_until(trial_ends_at=None, subscription=None, grace=GRACE) is None


def test_refunded_subscription_revokes_immediately() -> None:
    state = access_state(user(), sub(SubscriptionStatus.REFUNDED), T, grace=GRACE)
    assert state.kind is AccessKind.NONE


def test_admin_bypass() -> None:
    admin = user(role=UserRole.ADMIN)
    assert access_state(admin, None, T, grace=GRACE).kind is AccessKind.ADMIN
    assert has_access(UserRole.ADMIN, None, T)
    assert not trial_available(admin, has_paid=False)


def test_trial_available_once_and_not_after_payment() -> None:
    assert trial_available(user(), has_paid=False)
    assert not trial_available(user(), has_paid=True)
    used = user(trial_started_at=T, trial_ends_at=T + timedelta(hours=1))
    assert not trial_available(used, has_paid=False)


def test_user_config_round_trip_and_effective_settings() -> None:
    system = ScreenerSettings(cooldown_sec=123, exclude_symbols={"XUSDT"})
    cfg = UserDetectionConfig.from_settings(system)
    changed = cfg.model_copy(update={"long_threshold_pct": 7.5, "long_interval_sec": 300})
    eff = changed.to_settings(system)
    assert eff.long_threshold_pct == 7.5 and eff.long_interval_sec == 300
    assert eff.cooldown_sec == 123 and eff.exclude_symbols == {"XUSDT"}
    assert eff.config_hash() != system.config_hash()
    assert cfg.to_settings(system).config_hash() == system.config_hash()


def test_user_config_rejects_bad_interval() -> None:
    with pytest.raises(ValueError):
        UserDetectionConfig(long_interval_sec=90)
    with pytest.raises(ValueError):
        UserDetectionConfig(long_threshold_pct=0)


def test_user_account_terms() -> None:
    u = user(terms_version="1", terms_accepted_at=T)
    assert u.terms_accepted("1")
    assert not u.terms_accepted("2")
    assert not replace(u, terms_accepted_at=None).terms_accepted("1")
