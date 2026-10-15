"""Migration 006 + CommercialRepository on real PostgreSQL (opt-in, destructive).

Set TEST_DATABASE_URL to a throwaway database whose name contains "test".
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from screener.domain.commercial import (
    PaymentInput,
    SubscriptionStatus,
    UserRole,
    UserSignalRecord,
    UserStatus,
)
from screener.domain.enums import Direction, HistoryQuality, SignalType
from screener.domain.models import RuleMatch
from screener.infrastructure.db.commercial_repo import CommercialRepository
from screener.infrastructure.db.models import PaymentRow, SubscriptionRow
from screener.infrastructure.db.session import create_engine, create_session_factory, session_scope

URL = os.getenv("TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    "test" not in URL.rsplit("/", 1)[-1],
    reason="TEST_DATABASE_URL to a *test* database is required",
)

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
GRACE = timedelta(minutes=15)
CFG = {"enable_long": True, "long_threshold_pct": 10.0}


def _alembic(action: str, revision: str) -> None:
    from alembic.config import Config

    from alembic import command

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", URL)
    getattr(command, action)(cfg, revision)


@pytest.fixture
async def sf():
    engine = create_engine(URL)
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await asyncio.to_thread(_alembic, "upgrade", "head")
    factory = create_session_factory(engine)
    yield factory
    await engine.dispose()


async def _user(repo: CommercialRepository, tg: int = 100, role=UserRole.USER):
    user, created = await repo.upsert_user(
        telegram_user_id=tg, chat_id=tg, language_code="ru", role=role, now=NOW
    )
    return user, created


async def _invoice(repo: CommercialRepository, user_id: int, payload: str, amount: int = 1500):
    await repo.create_invoice(
        payload=payload,
        user_id=user_id,
        plan_code="pro_30d",
        amount_stars=amount,
        currency="XTR",
        now=NOW,
        expires_at=NOW + timedelta(hours=24),
    )


def _pay(payload: str, charge: str, *, tg=100, amount=1500, first=True, exp=None):
    return PaymentInput(
        telegram_user_id=tg,
        currency="XTR",
        total_amount=amount,
        invoice_payload=payload,
        telegram_payment_charge_id=charge,
        is_recurring=True,
        is_first_recurring=first,
        subscription_expiration_date=exp or NOW + timedelta(days=30),
    )


async def test_migration_downgrade_upgrade_cycle(sf) -> None:
    await asyncio.to_thread(_alembic, "downgrade", "005")
    async with session_scope(sf) as session:
        exists = await session.execute(text("SELECT to_regclass('public.users')"))
        assert exists.scalar_one() is None
        # 005 schema untouched.
        assert (
            await session.execute(text("SELECT to_regclass('public.trading_executions')"))
        ).scalar_one() is not None
    await asyncio.to_thread(_alembic, "upgrade", "head")


async def test_upsert_user_registers_once_without_trial(sf) -> None:
    repo = CommercialRepository(sf)
    user, created = await _user(repo)
    assert created and user.trial_started_at is None and user.access_until is None
    again, created2 = await _user(repo)
    assert not created2 and again.id == user.id
    admin, _ = await _user(repo, tg=1, role=UserRole.ADMIN)
    assert admin.is_admin and not admin.monitoring_enabled
    assert user.monitoring_enabled


async def test_bot_blocked_cleared_on_next_contact(sf) -> None:
    repo = CommercialRepository(sf)
    user, _ = await _user(repo)
    await repo.mark_bot_blocked(user.id, NOW)
    assert (await repo.get_user(user.id)).status is UserStatus.BOT_BLOCKED
    again, _ = await _user(repo)
    assert again.status is UserStatus.ACTIVE


async def test_trial_is_atomic_and_one_time(sf) -> None:
    repo = CommercialRepository(sf)
    user, _ = await _user(repo)
    results = await asyncio.gather(
        *(
            repo.start_trial(user.id, now=NOW, duration=timedelta(hours=24), grace=GRACE)
            for _ in range(5)
        )
    )
    started = [r for r in results if r is not None]
    assert len(started) == 1
    assert started[0].access_until == NOW + timedelta(hours=24)
    assert (
        await repo.start_trial(user.id, now=NOW, duration=timedelta(hours=24), grace=GRACE)
    ) is None


async def test_profile_versions_are_immutable_and_sequential(sf) -> None:
    repo = CommercialRepository(sf)
    user, _ = await _user(repo)
    assert await repo.ensure_profile(user.id, config=CFG, schema_version=1, config_hash="h1")
    assert not await repo.ensure_profile(user.id, config=CFG, schema_version=1, config_hash="h1")
    no = await repo.add_profile_version(
        user.id, config={**CFG, "long_threshold_pct": 5.0}, schema_version=1, config_hash="h2",
        now=NOW,
    )
    assert no == 2
    stored = await repo.get_user_config(user.id)
    assert stored.version_no == 2 and stored.config["long_threshold_pct"] == 5.0


async def test_load_eligible_respects_access_and_monitoring(sf) -> None:
    repo = CommercialRepository(sf)
    paid, _ = await _user(repo, tg=100)
    idle, _ = await _user(repo, tg=101)
    admin, _ = await _user(repo, tg=1, role=UserRole.ADMIN)
    for u in (paid, idle, admin):
        await repo.ensure_profile(u.id, config=CFG, schema_version=1, config_hash="h")
    await repo.start_trial(paid.id, now=NOW, duration=timedelta(hours=1), grace=GRACE)
    await repo.set_monitoring(admin.id, True, NOW)
    ids = {u.id for u, _ in await repo.load_eligible(NOW)}
    assert ids == {paid.id, admin.id}
    ids_later = {u.id for u, _ in await repo.load_eligible(NOW + timedelta(hours=2))}
    assert ids_later == {admin.id}


async def test_successful_payment_idempotent_under_concurrency(sf) -> None:
    repo = CommercialRepository(sf)
    user, _ = await _user(repo)
    await _invoice(repo, user.id, "ps1_a")
    pay = _pay("ps1_a", "charge-1")
    results = await asyncio.gather(
        *(repo.apply_successful_payment(pay, now=NOW, grace=GRACE) for _ in range(4))
    )
    applied = [r for r in results if not r.duplicate]
    assert len(applied) == 1 and applied[0].decision.applied
    assert applied[0].user.access_until == NOW + timedelta(days=30) + GRACE
    async with session_scope(sf) as session:
        assert (await session.execute(select(func.count()).select_from(PaymentRow))).scalar() == 1
        assert (
            await session.execute(select(func.count()).select_from(SubscriptionRow))
        ).scalar() == 1


async def test_renewal_extends_and_wrong_amount_is_anomaly(sf) -> None:
    repo = CommercialRepository(sf)
    user, _ = await _user(repo)
    await _invoice(repo, user.id, "ps1_a")
    await repo.apply_successful_payment(_pay("ps1_a", "c1"), now=NOW, grace=GRACE)
    renewal = _pay("ps1_a", "c2", first=False, exp=NOW + timedelta(days=60))
    res = await repo.apply_successful_payment(renewal, now=NOW + timedelta(days=30), grace=GRACE)
    assert res.decision.applied and res.subscription.current_period_end == NOW + timedelta(days=60)
    bad = await repo.apply_successful_payment(
        _pay("ps1_a", "c3", first=False, amount=1), now=NOW, grace=GRACE
    )
    assert bad.decision.anomaly == "amount_mismatch"


async def test_foreign_payload_and_second_subscription_are_anomalies(sf) -> None:
    repo = CommercialRepository(sf)
    alice, _ = await _user(repo, tg=100)
    bob, _ = await _user(repo, tg=200)
    await _invoice(repo, alice.id, "ps1_alice")
    stolen = await repo.apply_successful_payment(
        _pay("ps1_alice", "x1", tg=200), now=NOW, grace=GRACE
    )
    assert stolen.decision.anomaly == "owner_mismatch"
    assert (await repo.get_user(bob.id)).access_until is None

    await repo.apply_successful_payment(_pay("ps1_alice", "a1"), now=NOW, grace=GRACE)
    await _invoice(repo, alice.id, "ps1_alice2")
    dup = await repo.apply_successful_payment(_pay("ps1_alice2", "a2"), now=NOW, grace=GRACE)
    assert dup.decision.anomaly == "duplicate_subscription"


async def test_one_live_subscription_per_user_enforced_by_db(sf) -> None:
    repo = CommercialRepository(sf)
    user, _ = await _user(repo)
    with pytest.raises(IntegrityError):
        async with session_scope(sf) as session:
            for i in range(2):
                session.add(
                    SubscriptionRow(
                        id=uuid4(),
                        user_id=user.id,
                        plan_code="pro_30d",
                        status="active",
                        invoice_payload=f"p{i}",
                        subscription_charge_id=f"c{i}",
                        current_period_end=NOW,
                    )
                )
            await session.flush()


async def test_cancel_keeps_access_until_period_end_then_expiry_claimed_once(sf) -> None:
    repo = CommercialRepository(sf)
    user, _ = await _user(repo)
    await _invoice(repo, user.id, "ps1_a")
    await repo.apply_successful_payment(_pay("ps1_a", "c1"), now=NOW, grace=GRACE)
    sub, account = await repo.set_cancel_at_period_end(
        user.id, cancel=True, now=NOW, grace=GRACE
    )
    assert sub.status is SubscriptionStatus.CANCELED
    assert account.access_until == NOW + timedelta(days=30)
    end = NOW + timedelta(days=30)
    assert await repo.claim_expired(end - timedelta(seconds=1)) == []
    assert await repo.expire_subscriptions(end, GRACE) == 1
    claimed = await repo.claim_expired(end)
    assert [u.id for u in claimed] == [user.id]
    assert await repo.claim_expired(end + timedelta(hours=1)) == []
    assert await repo.get_live_subscription(user.id) is None


async def test_resume_restores_grace(sf) -> None:
    repo = CommercialRepository(sf)
    user, _ = await _user(repo)
    await _invoice(repo, user.id, "ps1_a")
    await repo.apply_successful_payment(_pay("ps1_a", "c1"), now=NOW, grace=GRACE)
    await repo.set_cancel_at_period_end(user.id, cancel=True, now=NOW, grace=GRACE)
    sub, account = await repo.set_cancel_at_period_end(
        user.id, cancel=False, now=NOW, grace=GRACE
    )
    assert sub.status is SubscriptionStatus.ACTIVE
    assert account.access_until == NOW + timedelta(days=30) + GRACE


async def test_refund_revokes_and_is_idempotent(sf) -> None:
    repo = CommercialRepository(sf)
    user, _ = await _user(repo)
    await _invoice(repo, user.id, "ps1_a")
    await repo.apply_successful_payment(_pay("ps1_a", "c1"), now=NOW, grace=GRACE)
    kw = dict(charge_id="c1", currency="XTR", total_amount=1500, invoice_payload="ps1_a")
    res = await repo.apply_refund(telegram_user_id=100, now=NOW, grace=GRACE, **kw)
    assert res.found and not res.already_refunded
    assert res.subscription.status is SubscriptionStatus.REFUNDED
    assert res.user.access_until is None
    again = await repo.apply_refund(telegram_user_id=100, now=NOW, grace=GRACE, **kw)
    assert again.already_refunded
    foreign = await repo.apply_refund(telegram_user_id=999, now=NOW, grace=GRACE, **kw)
    assert not foreign.found


async def test_trial_unavailable_after_payment(sf) -> None:
    repo = CommercialRepository(sf)
    user, _ = await _user(repo)
    await _invoice(repo, user.id, "ps1_a")
    await repo.apply_successful_payment(_pay("ps1_a", "c1"), now=NOW, grace=GRACE)
    assert await repo.has_paid(user.id)
    assert (
        await repo.start_trial(user.id, now=NOW, duration=timedelta(hours=24), grace=GRACE)
    ) is None


async def test_user_signal_events_unique_and_purged(sf) -> None:
    from screener.domain.commercial import GroupSignal

    repo = CommercialRepository(sf)
    user, _ = await _user(repo)
    match = RuleMatch(
        SignalType.PUMP, 60, 5.0, 6.0, 1.0, 1.06, 0, HistoryQuality.LIVE
    )
    signal = GroupSignal("g" * 64, "AUSDT", Direction.UP, match, (match,), NOW)
    await repo.save_commercial_signals([signal])
    rec = UserSignalRecord(user.id, signal.id, "sent", None, 1, NOW, NOW)
    await repo.save_user_signal_events([rec, rec])
    await repo.save_user_signal_events([rec])
    async with session_scope(sf) as session:
        n = await session.execute(text("SELECT count(*) FROM user_signal_events"))
        assert n.scalar_one() == 1
    assert await repo.purge_signals_before(NOW + timedelta(days=1)) == 1
    async with session_scope(sf) as session:
        n = await session.execute(text("SELECT count(*) FROM user_signal_events"))
        assert n.scalar_one() == 0


async def test_admin_stats(sf) -> None:
    repo = CommercialRepository(sf)
    user, _ = await _user(repo)
    await repo.start_trial(user.id, now=NOW, duration=timedelta(hours=24), grace=GRACE)
    await _invoice(repo, user.id, "ps1_a")
    await repo.apply_successful_payment(_pay("ps1_a", "c1"), now=NOW, grace=GRACE)
    stats = await repo.admin_stats(NOW)
    assert stats["users_total"] == 1
    assert stats["subscriptions_active"] == 1
    assert stats["revenue_stars_30d"] == 1500
    assert stats["trial_converted"] == 1


async def test_sync_admin_roles(sf) -> None:
    repo = CommercialRepository(sf)
    old_admin, _ = await _user(repo, tg=1, role=UserRole.ADMIN)
    user, _ = await _user(repo, tg=2)
    await repo.sync_admin_roles([2])
    assert (await repo.get_user(old_admin.id)).role is UserRole.USER
    assert (await repo.get_user(user.id)).role is UserRole.ADMIN
