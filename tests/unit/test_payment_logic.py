from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from screener.application.clock import FakeClock
from screener.application.commercial import payment_service as ps
from screener.application.commercial.payment_service import (
    InvoiceError,
    PaymentService,
    SubscriptionChangeError,
    new_payload,
)
from screener.application.commercial.subscription_service import (
    PERIOD,
    PaymentAction,
    decide_payment,
    expired_by,
)
from screener.config import CommercialSettings
from screener.domain.commercial import (
    InvoiceInfo,
    InvoiceStatus,
    PaymentInput,
    SubscriptionInfo,
    SubscriptionStatus,
    UserAccount,
    UserRole,
    UserStatus,
)

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
GRACE = timedelta(minutes=15)


def _user(uid: int = 1, tg: int = 100, **kw) -> UserAccount:
    base = dict(
        id=uid,
        telegram_user_id=tg,
        chat_id=tg,
        role=UserRole.USER,
        status=UserStatus.ACTIVE,
        monitoring_enabled=True,
        terms_version="1",
        terms_accepted_at=NOW,
        trial_started_at=None,
        trial_ends_at=None,
        access_until=None,
        expiry_notified_until=None,
    )
    base.update(kw)
    return UserAccount(**base)


def _invoice(user_id: int = 1, amount: int = 1500, **kw) -> InvoiceInfo:
    base = dict(
        payload="ps1_x",
        user_id=user_id,
        plan_code="pro_30d",
        amount_stars=amount,
        currency="XTR",
        status=InvoiceStatus.CREATED,
        created_at=NOW,
        expires_at=NOW + timedelta(hours=24),
    )
    base.update(kw)
    return InvoiceInfo(**base)


def _sub(user_id: int = 1, **kw) -> SubscriptionInfo:
    base = dict(
        id=uuid4(),
        user_id=user_id,
        plan_code="pro_30d",
        status=SubscriptionStatus.ACTIVE,
        invoice_payload="ps1_x",
        subscription_charge_id="ch1",
        current_period_end=NOW + PERIOD,
        cancel_at_period_end=False,
    )
    base.update(kw)
    return SubscriptionInfo(**base)


def _pay(**kw) -> PaymentInput:
    base = dict(
        telegram_user_id=100,
        currency="XTR",
        total_amount=1500,
        invoice_payload="ps1_x",
        telegram_payment_charge_id="ch1",
        is_recurring=True,
        is_first_recurring=True,
    )
    base.update(kw)
    return PaymentInput(**base)


def _decide(payment, **kw):
    args = dict(
        owner=_user(),
        invoice=_invoice(),
        subscription_for_payload=None,
        live_subscription=None,
        now=NOW,
    )
    args.update(kw)
    return decide_payment(payment, **args)


# --- decide_payment ------------------------------------------------------------------


def test_first_payment_creates_subscription_with_telegram_expiry() -> None:
    exp = NOW + timedelta(days=30, minutes=1)
    d = _decide(_pay(subscription_expiration_date=exp))
    assert d.applied and d.action is PaymentAction.CREATE and d.period_end == exp


def test_first_payment_without_expiry_uses_30_days() -> None:
    assert _decide(_pay()).period_end == NOW + PERIOD


@pytest.mark.parametrize(
    ("payment", "kw", "reason"),
    [
        (_pay(currency="USD"), {}, "currency"),
        (_pay(), {"owner": None}, "unknown_user"),
        (_pay(), {"invoice": None}, "unknown_payload"),
        (_pay(), {"invoice": _invoice(user_id=2)}, "owner_mismatch"),
        (_pay(total_amount=1), {}, "amount_mismatch"),
        (_pay(), {"subscription_for_payload": _sub()}, "payload_reused"),
        (_pay(), {"live_subscription": _sub(invoice_payload="other")}, "duplicate_subscription"),
    ],
)
def test_initial_payment_anomalies(payment, kw, reason) -> None:
    d = _decide(payment, **kw)
    assert not d.applied and d.anomaly == reason and d.action is PaymentAction.NONE


def test_renewal_extends_and_never_shortens() -> None:
    sub = _sub()
    later = sub.current_period_end + PERIOD
    d = _decide(
        _pay(
            is_first_recurring=False,
            telegram_payment_charge_id="ch2",
            subscription_expiration_date=later,
        ),
        subscription_for_payload=sub,
        live_subscription=sub,
    )
    assert d.action is PaymentAction.EXTEND and d.period_end == later
    stale = _decide(
        _pay(is_first_recurring=False, subscription_expiration_date=NOW),
        subscription_for_payload=sub,
        live_subscription=sub,
    )
    assert stale.period_end == sub.current_period_end


def test_renewal_reactivates_expired_subscription() -> None:
    sub = _sub(status=SubscriptionStatus.EXPIRED, current_period_end=NOW - timedelta(hours=1))
    d = _decide(_pay(is_first_recurring=False), subscription_for_payload=sub)
    assert d.applied and d.period_end == sub.current_period_end + PERIOD


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"subscription_for_payload": None}, "unknown_subscription"),
        ({"subscription_for_payload": _sub(user_id=2)}, "owner_mismatch"),
        (
            {"subscription_for_payload": _sub(status=SubscriptionStatus.REFUNDED)},
            "renewal_after_refund",
        ),
    ],
)
def test_renewal_anomalies(kw, reason) -> None:
    assert _decide(_pay(is_first_recurring=False), **kw).anomaly == reason


def test_expired_by_respects_grace_only_for_auto_renew() -> None:
    sub = _sub(current_period_end=NOW)
    assert not expired_by(sub, NOW + GRACE - timedelta(seconds=1), grace=GRACE)
    assert expired_by(sub, NOW + GRACE, grace=GRACE)
    canceled = replace(sub, status=SubscriptionStatus.CANCELED, cancel_at_period_end=True)
    assert expired_by(canceled, NOW, grace=GRACE)


def test_payloads_are_unique_opaque_and_short() -> None:
    payloads = {new_payload() for _ in range(1000)}
    assert len(payloads) == 1000
    assert all(p.startswith("ps1_") and len(p.encode()) <= 64 for p in payloads)


# --- PaymentService with fakes ---------------------------------------------------------


class FakeStore:
    def __init__(self) -> None:
        self.invoices: dict[str, InvoiceInfo] = {}
        self.users = {100: _user()}
        self.live: SubscriptionInfo | None = None
        self.invoice_count = 0
        self.apply_failures = 0
        self.applied: list[PaymentInput] = []
        self.slow = False

    async def get_live_subscription(self, user_id):
        return self.live

    async def count_invoices_since(self, user_id, since):
        return self.invoice_count

    async def create_invoice(self, **kw):
        self.invoice_count += 1
        self.invoices[kw["payload"]] = _invoice(payload=kw["payload"], expires_at=kw["expires_at"])

    async def get_invoice(self, payload):
        if self.slow:
            await asyncio.sleep(10)
        return self.invoices.get(payload)

    async def get_user_by_telegram(self, tg):
        return self.users.get(tg)

    async def mark_invoice_pre_checkout(self, payload):
        return None

    async def apply_successful_payment(self, payment, *, now, grace):
        if self.apply_failures:
            self.apply_failures -= 1
            raise ConnectionError("db down")
        self.applied.append(payment)
        decision = SimpleNamespace(applied=True, anomaly=None, period_end=now + PERIOD)
        return SimpleNamespace(duplicate=False, decision=decision, user=_user())

    async def set_cancel_at_period_end(self, user_id, *, cancel, now, grace):
        assert self.live is not None
        self.live = replace(self.live, cancel_at_period_end=cancel)
        return self.live, _user()


class FakeGateway:
    def __init__(self) -> None:
        self.links = 0
        self.canceled: list[tuple[int, str, bool]] = []
        self.fail = False

    async def create_subscription_link(self, **kw):
        self.links += 1
        assert kw["period_sec"] == 2_592_000
        return "https://t.me/$x"

    async def set_subscription_canceled(self, tg, charge, canceled):
        if self.fail:
            raise RuntimeError("telegram")
        self.canceled.append((tg, charge, canceled))

    async def refund(self, tg, charge):
        return None


class FakeUsers:
    def __init__(self) -> None:
        self.settings = CommercialSettings(_env_file=None)  # type: ignore[call-arg]
        self.grace = GRACE
        self.applied: list[UserAccount] = []

    async def apply_account(self, user):
        self.applied.append(user)


def _service(store=None, gateway=None, alerts=None):
    store = store or FakeStore()
    gateway = gateway or FakeGateway()
    users = FakeUsers()

    async def alert(text):
        if alerts is not None:
            alerts.append(text)

    svc = PaymentService(
        store, users, gateway, FakeClock(int(NOW.timestamp() * 1000)), alert_admins=alert
    )
    return svc, store, gateway, users


async def test_invoice_guards() -> None:
    svc, store, gw, _ = _service()
    assert (await svc.create_invoice(_user(role=UserRole.ADMIN))).error is InvoiceError.ADMIN
    no_terms = _user(terms_accepted_at=None)
    assert (await svc.create_invoice(no_terms)).error is InvoiceError.TERMS_REQUIRED
    ok = await svc.create_invoice(_user())
    assert ok.link and gw.links == 1
    store.invoice_count = 5
    assert (await svc.create_invoice(_user())).error is InvoiceError.RATE_LIMITED
    store.invoice_count = 0
    store.live = _sub()
    assert (await svc.create_invoice(_user())).error is InvoiceError.ALREADY_SUBSCRIBED


async def test_pre_checkout_validates_and_times_out(monkeypatch) -> None:
    svc, store, _, _ = _service()
    await svc.create_invoice(_user())
    payload = next(iter(store.invoices))
    ok = dict(telegram_user_id=100, payload=payload, currency="XTR", total_amount=1500)
    assert await svc.pre_checkout(**ok) is None
    assert await svc.pre_checkout(**{**ok, "total_amount": 1}) is not None
    assert await svc.pre_checkout(**{**ok, "payload": "ps1_unknown"}) is not None
    assert await svc.pre_checkout(**{**ok, "telegram_user_id": 555}) is not None
    monkeypatch.setattr(ps, "PRE_CHECKOUT_TIMEOUT_SEC", 0.05)
    store.slow = True
    assert await svc.pre_checkout(**ok) is not None  # answered in time, rejected


async def test_successful_payment_retries_db_errors(monkeypatch) -> None:
    monkeypatch.setattr(ps, "APPLY_RETRY_BASE_SEC", 0.001)
    alerts: list[str] = []
    svc, store, _, users = _service(alerts=alerts)
    store.apply_failures = 2
    outcome = await svc.successful_payment(_pay())
    assert outcome.applied and len(store.applied) == 1 and len(users.applied) == 1
    assert alerts == []


async def test_successful_payment_alerts_admin_when_db_stays_down(monkeypatch) -> None:
    monkeypatch.setattr(ps, "APPLY_RETRY_BASE_SEC", 0.001)
    alerts: list[str] = []
    svc, store, _, _ = _service(alerts=alerts)
    store.apply_failures = 99
    with pytest.raises(ConnectionError):
        await svc.successful_payment(_pay())
    assert len(alerts) == 1 and "ch1" in alerts[0]


async def test_cancel_changes_telegram_first_and_keeps_state_on_failure() -> None:
    svc, store, gw, _ = _service()
    store.live = _sub()
    gw.fail = True
    _, err = await svc.cancel_autorenew(_user())
    assert err is SubscriptionChangeError.GATEWAY and store.live.cancel_at_period_end is False
    gw.fail = False
    sub, err = await svc.cancel_autorenew(_user())
    assert err is None and sub.cancel_at_period_end and gw.canceled == [(100, "ch1", True)]
    _, err = await svc.cancel_autorenew(_user())
    assert err is SubscriptionChangeError.ALREADY
    sub, err = await svc.resume_autorenew(_user())
    assert err is None and not sub.cancel_at_period_end


async def test_cancel_without_subscription() -> None:
    svc, *_ = _service()
    _, err = await svc.cancel_autorenew(_user())
    assert err is SubscriptionChangeError.NO_SUBSCRIPTION
