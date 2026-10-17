from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, case, delete, exists, func, literal_column, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from screener.application.commercial.access import compute_access_until
from screener.application.commercial.subscription_service import (
    PaymentAction,
    PaymentDecision,
    decide_payment,
)
from screener.domain.commercial import (
    LIVE_SUBSCRIPTION_STATUSES,
    InvoiceInfo,
    InvoiceStatus,
    PaymentInput,
    PaymentKind,
    PaymentStatus,
    SubscriptionInfo,
    SubscriptionStatus,
    UserAccount,
    UserRole,
    UserSignalRecord,
    UserStatus,
)
from screener.domain.exchanges import normalize_exchanges
from screener.infrastructure.db.models import (
    CommercialSignalRow,
    PaymentInvoiceRow,
    PaymentRow,
    SubscriptionRow,
    UserMonitoringProfileRow,
    UserMonitoringProfileVersionRow,
    UserProfileTemplateRow,
    UserRow,
    UserSignalEventRow,
)
from screener.infrastructure.db.session import session_scope

_LIVE = tuple(s.value for s in LIVE_SUBSCRIPTION_STATUSES)


@dataclass(slots=True, frozen=True)
class StoredConfig:
    config: dict[str, Any]
    schema_version: int
    version_no: int


@dataclass(slots=True, frozen=True)
class PaymentResult:
    duplicate: bool
    decision: PaymentDecision | None
    user: UserAccount | None = None
    subscription: SubscriptionInfo | None = None


@dataclass(slots=True, frozen=True)
class PaymentRef:
    user_id: int
    telegram_user_id: int
    total_amount: int
    currency: str
    invoice_payload: str


@dataclass(slots=True, frozen=True)
class RefundResult:
    found: bool
    already_refunded: bool
    user: UserAccount | None = None
    subscription: SubscriptionInfo | None = None


def _user(row: Any) -> UserAccount:
    return UserAccount(
        id=row.id,
        telegram_user_id=row.telegram_user_id,
        chat_id=row.chat_id,
        role=UserRole(row.role),
        status=UserStatus(row.status),
        monitoring_enabled=row.monitoring_enabled,
        terms_version=row.terms_version,
        terms_accepted_at=row.terms_accepted_at,
        trial_started_at=row.trial_started_at,
        trial_ends_at=row.trial_ends_at,
        access_until=row.access_until,
        expiry_notified_until=row.expiry_notified_until,
        created_at=row.created_at,
        language_code=row.language_code,
        enabled_exchanges=normalize_exchanges(getattr(row, "enabled_exchanges", None)),
    )


def _sub(row: SubscriptionRow) -> SubscriptionInfo:
    return SubscriptionInfo(
        id=row.id,
        user_id=row.user_id,
        plan_code=row.plan_code,
        status=SubscriptionStatus(row.status),
        invoice_payload=row.invoice_payload,
        subscription_charge_id=row.subscription_charge_id,
        current_period_end=row.current_period_end,
        cancel_at_period_end=row.cancel_at_period_end,
        started_at=row.started_at,
    )


def _invoice(row: PaymentInvoiceRow) -> InvoiceInfo:
    return InvoiceInfo(
        payload=row.payload,
        user_id=row.user_id,
        plan_code=row.plan_code,
        amount_stars=row.amount_stars,
        currency=row.currency,
        status=InvoiceStatus(row.status),
        created_at=row.created_at,
        expires_at=row.expires_at,
    )


_USER_COLS = tuple(UserRow.__table__.c)


class CommercialRepository:
    """All commercial state transitions; each public method is one transaction."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    # --- users ---------------------------------------------------------------

    async def upsert_user(
        self,
        *,
        telegram_user_id: int,
        chat_id: int,
        language_code: str | None,
        role: UserRole,
        now: datetime,
    ) -> tuple[UserAccount, bool]:
        """Register on first contact (never starts a trial); refresh on later updates.

        Writing to the bot again clears ``bot_blocked``; ``banned`` is sticky.
        """
        lang = (language_code or "")[:8] or None
        stmt = (
            insert(UserRow)
            .values(
                telegram_user_id=telegram_user_id,
                chat_id=chat_id,
                language_code=lang,
                role=role.value,
                # Admins already get the internal admin feed; commercial copies are opt-in.
                monitoring_enabled=role is UserRole.USER,
                created_at=now,
                updated_at=now,
                last_seen_at=now,
            )
            .on_conflict_do_update(
                index_elements=[UserRow.telegram_user_id],
                set_={
                    "chat_id": chat_id,
                    "language_code": func.coalesce(lang, UserRow.language_code),
                    "role": role.value,
                    "last_seen_at": now,
                    "status": case(
                        (UserRow.status == UserStatus.BOT_BLOCKED.value, UserStatus.ACTIVE.value),
                        else_=UserRow.status,
                    ),
                    "bot_blocked_at": case(
                        (UserRow.status == UserStatus.BOT_BLOCKED.value, None),
                        else_=UserRow.bot_blocked_at,
                    ),
                },
            )
            .returning(*_USER_COLS, literal_column("(xmax = 0)").label("inserted"))
        )
        async with session_scope(self._sf) as session:
            row = (await session.execute(stmt)).one()
            return _user(row), bool(row.inserted)

    async def sync_admin_roles(self, admin_telegram_ids: Sequence[int]) -> None:
        ids = list(admin_telegram_ids)
        async with session_scope(self._sf) as session:
            demote = update(UserRow).where(UserRow.role == UserRole.ADMIN.value)
            if ids:
                demote = demote.where(UserRow.telegram_user_id.not_in(ids))
            await session.execute(demote.values(role=UserRole.USER.value))
            if ids:
                await session.execute(
                    update(UserRow)
                    .where(UserRow.telegram_user_id.in_(ids))
                    .values(role=UserRole.ADMIN.value)
                )

    async def get_user(self, user_id: int) -> UserAccount | None:
        async with session_scope(self._sf) as session:
            row = await session.get(UserRow, user_id)
            return None if row is None else _user(row)

    async def get_user_by_telegram(self, telegram_user_id: int) -> UserAccount | None:
        async with session_scope(self._sf) as session:
            row = (
                await session.execute(
                    select(UserRow).where(UserRow.telegram_user_id == telegram_user_id)
                )
            ).scalar_one_or_none()
            return None if row is None else _user(row)

    async def set_monitoring(self, user_id: int, enabled: bool, now: datetime) -> None:
        async with session_scope(self._sf) as session:
            await session.execute(
                update(UserRow)
                .where(UserRow.id == user_id)
                .values(monitoring_enabled=enabled, updated_at=now)
            )

    async def set_exchanges(self, user_id: int, exchanges: Sequence[str], now: datetime) -> None:
        async with session_scope(self._sf) as session:
            await session.execute(
                update(UserRow)
                .where(UserRow.id == user_id)
                .values(enabled_exchanges=list(exchanges), updated_at=now)
            )

    async def accept_terms(self, user_id: int, version: str, now: datetime) -> None:
        async with session_scope(self._sf) as session:
            await session.execute(
                update(UserRow)
                .where(UserRow.id == user_id)
                .values(terms_version=version, terms_accepted_at=now, updated_at=now)
            )

    async def mark_bot_blocked(self, user_id: int, now: datetime) -> None:
        async with session_scope(self._sf) as session:
            await session.execute(
                update(UserRow)
                .where(UserRow.id == user_id, UserRow.status == UserStatus.ACTIVE.value)
                .values(status=UserStatus.BOT_BLOCKED.value, bot_blocked_at=now, updated_at=now)
            )

    # --- profiles --------------------------------------------------------------

    async def get_template(self, code: str) -> tuple[dict[str, Any], int, str] | None:
        async with session_scope(self._sf) as session:
            row = await session.get(UserProfileTemplateRow, code)
            if row is None:
                return None
            return dict(row.config), row.config_schema_version, row.config_hash

    async def upsert_template(
        self,
        code: str,
        *,
        config: dict[str, Any],
        schema_version: int,
        config_hash: str,
        source_profile_version_id: UUID | None,
        now: datetime,
    ) -> None:
        values = {
            "config": config,
            "config_schema_version": schema_version,
            "config_hash": config_hash,
            "source_profile_version_id": source_profile_version_id,
            "updated_at": now,
        }
        stmt = insert(UserProfileTemplateRow).values(code=code, **values)
        stmt = stmt.on_conflict_do_update(index_elements=[UserProfileTemplateRow.code], set_=values)
        async with session_scope(self._sf) as session:
            await session.execute(stmt)

    async def ensure_profile(
        self, user_id: int, *, config: dict[str, Any], schema_version: int, config_hash: str
    ) -> bool:
        """Create the user's profile with version 1; no-op when it already exists."""
        async with session_scope(self._sf) as session:
            profile_id = (
                await session.execute(
                    insert(UserMonitoringProfileRow)
                    .values(id=uuid4(), user_id=user_id)
                    .on_conflict_do_nothing(index_elements=[UserMonitoringProfileRow.user_id])
                    .returning(UserMonitoringProfileRow.id)
                )
            ).scalar_one_or_none()
            if profile_id is None:
                return False
            version_id = uuid4()
            session.add(
                UserMonitoringProfileVersionRow(
                    id=version_id,
                    profile_id=profile_id,
                    version_no=1,
                    config=config,
                    config_schema_version=schema_version,
                    config_hash=config_hash,
                )
            )
            await session.flush()
            await session.execute(
                update(UserMonitoringProfileRow)
                .where(UserMonitoringProfileRow.id == profile_id)
                .values(current_version_id=version_id)
            )
            return True

    async def add_profile_version(
        self,
        user_id: int,
        *,
        config: dict[str, Any],
        schema_version: int,
        config_hash: str,
        now: datetime,
    ) -> int:
        async with session_scope(self._sf) as session:
            profile = (
                await session.execute(
                    select(UserMonitoringProfileRow)
                    .where(UserMonitoringProfileRow.user_id == user_id)
                    .with_for_update()
                )
            ).scalar_one()
            last = (
                await session.execute(
                    select(
                        func.coalesce(func.max(UserMonitoringProfileVersionRow.version_no), 0)
                    ).where(UserMonitoringProfileVersionRow.profile_id == profile.id)
                )
            ).scalar_one()
            version_id = uuid4()
            session.add(
                UserMonitoringProfileVersionRow(
                    id=version_id,
                    profile_id=profile.id,
                    version_no=last + 1,
                    config=config,
                    config_schema_version=schema_version,
                    config_hash=config_hash,
                )
            )
            await session.flush()
            profile.current_version_id = version_id
            profile.updated_at = now
            return int(last + 1)

    async def get_user_config(self, user_id: int) -> StoredConfig | None:
        stmt = (
            select(
                UserMonitoringProfileVersionRow.config,
                UserMonitoringProfileVersionRow.config_schema_version,
                UserMonitoringProfileVersionRow.version_no,
            )
            .join(
                UserMonitoringProfileRow,
                UserMonitoringProfileRow.current_version_id == UserMonitoringProfileVersionRow.id,
            )
            .where(UserMonitoringProfileRow.user_id == user_id)
        )
        async with session_scope(self._sf) as session:
            row = (await session.execute(stmt)).one_or_none()
            if row is None:
                return None
            return StoredConfig(dict(row.config), row.config_schema_version, row.version_no)

    async def load_eligible(self, now: datetime) -> list[tuple[UserAccount, StoredConfig]]:
        """Users who may receive signals right now, with their current config."""
        stmt = (
            select(
                *_USER_COLS,
                UserMonitoringProfileVersionRow.config,
                UserMonitoringProfileVersionRow.config_schema_version,
                UserMonitoringProfileVersionRow.version_no,
            )
            .join(UserMonitoringProfileRow, UserMonitoringProfileRow.user_id == UserRow.id)
            .join(
                UserMonitoringProfileVersionRow,
                UserMonitoringProfileRow.current_version_id == UserMonitoringProfileVersionRow.id,
            )
            .where(
                UserRow.status == UserStatus.ACTIVE.value,
                UserRow.monitoring_enabled.is_(True),
                or_(UserRow.role == UserRole.ADMIN.value, UserRow.access_until > now),
            )
        )
        async with session_scope(self._sf) as session:
            rows = (await session.execute(stmt)).all()
        return [
            (
                _user(row),
                StoredConfig(dict(row.config), row.config_schema_version, row.version_no),
            )
            for row in rows
        ]

    # --- access ------------------------------------------------------------------

    async def _live_subscription(
        self, session: AsyncSession, user_id: int, *, lock: bool = False
    ) -> SubscriptionRow | None:
        stmt = select(SubscriptionRow).where(
            SubscriptionRow.user_id == user_id, SubscriptionRow.status.in_(_LIVE)
        )
        if lock:
            stmt = stmt.with_for_update()
        return (await session.execute(stmt)).scalar_one_or_none()

    async def _recompute_access(
        self, session: AsyncSession, user_id: int, now: datetime, grace: timedelta
    ) -> UserAccount:
        user = (
            await session.execute(select(UserRow).where(UserRow.id == user_id).with_for_update())
        ).scalar_one()
        sub = await self._live_subscription(session, user_id)
        user.access_until = compute_access_until(
            trial_ends_at=user.trial_ends_at,
            subscription=None if sub is None else _sub(sub),
            grace=grace,
        )
        user.updated_at = now
        await session.flush()
        return _user(user)

    async def has_paid(self, user_id: int) -> bool:
        async with session_scope(self._sf) as session:
            return bool(
                (
                    await session.execute(
                        select(
                            exists().where(
                                PaymentRow.user_id == user_id,
                                PaymentRow.status == PaymentStatus.APPLIED.value,
                                PaymentRow.kind != PaymentKind.REFUND.value,
                            )
                        )
                    )
                ).scalar_one()
            )

    async def start_trial(
        self, user_id: int, *, now: datetime, duration: timedelta, grace: timedelta
    ) -> UserAccount | None:
        """Atomic one-time trial; ``None`` when already used or the user has paid."""
        paid = exists().where(
            PaymentRow.user_id == user_id,
            PaymentRow.status == PaymentStatus.APPLIED.value,
        )
        async with session_scope(self._sf) as session:
            started = (
                await session.execute(
                    update(UserRow)
                    .where(
                        UserRow.id == user_id,
                        UserRow.trial_started_at.is_(None),
                        UserRow.role == UserRole.USER.value,
                        ~paid,
                    )
                    .values(trial_started_at=now, trial_ends_at=now + duration, updated_at=now)
                    .returning(UserRow.id)
                )
            ).scalar_one_or_none()
            if started is None:
                return None
            return await self._recompute_access(session, user_id, now, grace)

    async def get_live_subscription(self, user_id: int) -> SubscriptionInfo | None:
        async with session_scope(self._sf) as session:
            row = await self._live_subscription(session, user_id)
            return None if row is None else _sub(row)

    async def set_cancel_at_period_end(
        self, user_id: int, *, cancel: bool, now: datetime, grace: timedelta
    ) -> tuple[SubscriptionInfo, UserAccount] | None:
        async with session_scope(self._sf) as session:
            row = await self._live_subscription(session, user_id, lock=True)
            if row is None:
                return None
            row.cancel_at_period_end = cancel
            status = SubscriptionStatus.CANCELED if cancel else SubscriptionStatus.ACTIVE
            row.status = status.value
            row.canceled_at = now if cancel else None
            row.updated_at = now
            await session.flush()
            user = await self._recompute_access(session, user_id, now, grace)
            return _sub(row), user

    async def expire_subscriptions(self, now: datetime, grace: timedelta) -> int:
        """Flip ended subscriptions to ``expired``. ``access_until`` already ends there."""
        ended = or_(
            and_(
                or_(
                    SubscriptionRow.status == SubscriptionStatus.CANCELED.value,
                    SubscriptionRow.cancel_at_period_end.is_(True),
                ),
                SubscriptionRow.current_period_end <= now,
            ),
            SubscriptionRow.current_period_end <= now - grace,
        )
        async with session_scope(self._sf) as session:
            result = await session.execute(
                update(SubscriptionRow)
                .where(SubscriptionRow.status.in_(_LIVE), ended)
                .values(status=SubscriptionStatus.EXPIRED.value, ended_at=now, updated_at=now)
                .returning(SubscriptionRow.id)
            )
            return len(result.all())

    async def claim_expired(self, now: datetime, *, limit: int = 500) -> list[UserAccount]:
        """Mark users whose access ended as notified; each access period is claimed once."""
        pending = (
            select(UserRow.id)
            .where(
                UserRow.role == UserRole.USER.value,
                UserRow.access_until.is_not(None),
                UserRow.access_until <= now,
                or_(
                    UserRow.expiry_notified_until.is_(None),
                    UserRow.expiry_notified_until < UserRow.access_until,
                ),
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        async with session_scope(self._sf) as session:
            rows = (
                await session.execute(
                    update(UserRow)
                    .where(UserRow.id.in_(pending.scalar_subquery()))
                    .values(expiry_notified_until=UserRow.access_until)
                    .returning(*_USER_COLS)
                )
            ).all()
            return [_user(r) for r in rows]

    # --- invoices / payments -----------------------------------------------------

    async def count_invoices_since(self, user_id: int, since: datetime) -> int:
        async with session_scope(self._sf) as session:
            return int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(PaymentInvoiceRow)
                        .where(
                            PaymentInvoiceRow.user_id == user_id,
                            PaymentInvoiceRow.created_at >= since,
                        )
                    )
                ).scalar_one()
            )

    async def create_invoice(
        self,
        *,
        payload: str,
        user_id: int,
        plan_code: str,
        amount_stars: int,
        currency: str,
        now: datetime,
        expires_at: datetime,
    ) -> None:
        async with session_scope(self._sf) as session:
            session.add(
                PaymentInvoiceRow(
                    payload=payload,
                    user_id=user_id,
                    plan_code=plan_code,
                    amount_stars=amount_stars,
                    currency=currency,
                    status=InvoiceStatus.CREATED.value,
                    created_at=now,
                    expires_at=expires_at,
                )
            )

    async def get_invoice(self, payload: str) -> InvoiceInfo | None:
        async with session_scope(self._sf) as session:
            row = await session.get(PaymentInvoiceRow, payload)
            return None if row is None else _invoice(row)

    async def mark_invoice_pre_checkout(self, payload: str) -> None:
        async with session_scope(self._sf) as session:
            await session.execute(
                update(PaymentInvoiceRow)
                .where(
                    PaymentInvoiceRow.payload == payload,
                    PaymentInvoiceRow.status == InvoiceStatus.CREATED.value,
                )
                .values(status=InvoiceStatus.PRE_CHECKOUT_OK.value)
            )

    async def apply_successful_payment(
        self, payment: PaymentInput, *, now: datetime, grace: timedelta
    ) -> PaymentResult:
        """Idempotent by ``(telegram_payment_charge_id, kind)``; one transaction."""
        kind = payment.kind
        async with session_scope(self._sf) as session:
            payment_id = (
                await session.execute(
                    insert(PaymentRow)
                    .values(
                        kind=kind.value,
                        invoice_payload=payment.invoice_payload[:64],
                        telegram_payment_charge_id=payment.telegram_payment_charge_id[:128],
                        currency=payment.currency[:3],
                        total_amount=payment.total_amount,
                        is_recurring=payment.is_recurring,
                        is_first_recurring=payment.is_first_recurring,
                        subscription_expiration_date=payment.subscription_expiration_date,
                        status=PaymentStatus.ANOMALY.value,
                        received_at=now,
                    )
                    .on_conflict_do_nothing(constraint="uq_payments_charge_kind")
                    .returning(PaymentRow.id)
                )
            ).scalar_one_or_none()
            if payment_id is None:
                return PaymentResult(duplicate=True, decision=None)

            owner_row = (
                await session.execute(
                    select(UserRow)
                    .where(UserRow.telegram_user_id == payment.telegram_user_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            invoice_row = (
                await session.execute(
                    select(PaymentInvoiceRow)
                    .where(PaymentInvoiceRow.payload == payment.invoice_payload)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            sub_row = (
                await session.execute(
                    select(SubscriptionRow)
                    .where(SubscriptionRow.invoice_payload == payment.invoice_payload)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            live_row = (
                None
                if owner_row is None
                else await self._live_subscription(session, owner_row.id, lock=True)
            )
            decision = decide_payment(
                payment,
                owner=None if owner_row is None else _user(owner_row),
                invoice=None if invoice_row is None else _invoice(invoice_row),
                subscription_for_payload=None if sub_row is None else _sub(sub_row),
                live_subscription=None if live_row is None else _sub(live_row),
                now=now,
            )
            values: dict[str, Any] = {"user_id": None if owner_row is None else owner_row.id}
            if not decision.applied:
                values["anomaly_reason"] = decision.anomaly
                await session.execute(
                    update(PaymentRow).where(PaymentRow.id == payment_id).values(**values)
                )
                return PaymentResult(
                    duplicate=False,
                    decision=decision,
                    user=None if owner_row is None else _user(owner_row),
                )

            assert owner_row is not None and decision.period_end is not None
            if decision.action is PaymentAction.CREATE:
                assert invoice_row is not None
                sub_row = SubscriptionRow(
                    id=uuid4(),
                    user_id=owner_row.id,
                    plan_code=invoice_row.plan_code,
                    status=SubscriptionStatus.ACTIVE.value,
                    invoice_payload=payment.invoice_payload,
                    subscription_charge_id=payment.telegram_payment_charge_id,
                    current_period_end=decision.period_end,
                    cancel_at_period_end=False,
                    started_at=now,
                    created_at=now,
                    updated_at=now,
                )
                session.add(sub_row)
                invoice_row.status = InvoiceStatus.PAID.value
                invoice_row.paid_at = now
            else:
                assert sub_row is not None
                sub_row.current_period_end = decision.period_end
                sub_row.status = SubscriptionStatus.ACTIVE.value
                sub_row.cancel_at_period_end = False
                sub_row.canceled_at = None
                sub_row.ended_at = None
                sub_row.updated_at = now
            await session.flush()
            values.update(subscription_id=sub_row.id, status=PaymentStatus.APPLIED.value)
            await session.execute(
                update(PaymentRow).where(PaymentRow.id == payment_id).values(**values)
            )
            user = await self._recompute_access(session, owner_row.id, now, grace)
            return PaymentResult(
                duplicate=False, decision=decision, user=user, subscription=_sub(sub_row)
            )

    async def find_payment(self, charge_id: str) -> PaymentRef | None:
        """The applied (non-refund) payment with this Telegram charge id."""
        stmt = (
            select(
                UserRow.id,
                UserRow.telegram_user_id,
                PaymentRow.total_amount,
                PaymentRow.currency,
                PaymentRow.invoice_payload,
            )
            .join(PaymentRow, PaymentRow.user_id == UserRow.id)
            .where(
                PaymentRow.telegram_payment_charge_id == charge_id,
                PaymentRow.kind != PaymentKind.REFUND.value,
                PaymentRow.status == PaymentStatus.APPLIED.value,
            )
            .limit(1)
        )
        async with session_scope(self._sf) as session:
            row = (await session.execute(stmt)).one_or_none()
            if row is None:
                return None
            return PaymentRef(
                user_id=row.id,
                telegram_user_id=row.telegram_user_id,
                total_amount=row.total_amount,
                currency=row.currency,
                invoice_payload=row.invoice_payload,
            )

    async def apply_refund(
        self,
        *,
        charge_id: str,
        telegram_user_id: int,
        currency: str,
        total_amount: int,
        invoice_payload: str,
        now: datetime,
        grace: timedelta,
    ) -> RefundResult:
        """Record a refund (idempotent) and revoke the subscription it paid for."""
        async with session_scope(self._sf) as session:
            original = (
                await session.execute(
                    select(PaymentRow).where(
                        PaymentRow.telegram_payment_charge_id == charge_id,
                        PaymentRow.kind != PaymentKind.REFUND.value,
                    )
                )
            ).scalar_one_or_none()
            if original is None or original.user_id is None:
                return RefundResult(found=False, already_refunded=False)
            owner = await session.get(UserRow, original.user_id)
            if owner is None or owner.telegram_user_id != telegram_user_id:
                return RefundResult(found=False, already_refunded=False)
            inserted = (
                await session.execute(
                    insert(PaymentRow)
                    .values(
                        user_id=original.user_id,
                        subscription_id=original.subscription_id,
                        kind=PaymentKind.REFUND.value,
                        invoice_payload=invoice_payload[:64] or original.invoice_payload,
                        telegram_payment_charge_id=charge_id,
                        currency=currency[:3],
                        total_amount=total_amount,
                        status=PaymentStatus.APPLIED.value,
                        received_at=now,
                    )
                    .on_conflict_do_nothing(constraint="uq_payments_charge_kind")
                    .returning(PaymentRow.id)
                )
            ).scalar_one_or_none()
            if inserted is None:
                return RefundResult(found=True, already_refunded=True)
            sub_info: SubscriptionInfo | None = None
            if original.subscription_id is not None:
                sub = await session.get(SubscriptionRow, original.subscription_id)
                if sub is not None and sub.status != SubscriptionStatus.REFUNDED.value:
                    sub.status = SubscriptionStatus.REFUNDED.value
                    sub.cancel_at_period_end = True
                    sub.ended_at = now
                    sub.updated_at = now
                    await session.flush()
                if sub is not None:
                    sub_info = _sub(sub)
            user = await self._recompute_access(session, original.user_id, now, grace)
            return RefundResult(
                found=True, already_refunded=False, user=user, subscription=sub_info
            )

    async def applied_charge_ids(self, since: datetime) -> set[str]:
        async with session_scope(self._sf) as session:
            rows = await session.execute(
                select(PaymentRow.telegram_payment_charge_id).where(
                    PaymentRow.received_at >= since,
                    PaymentRow.kind != PaymentKind.REFUND.value,
                )
            )
            return {r[0] for r in rows.all()}

    # --- user signals ---------------------------------------------------------------

    async def save_commercial_signals(self, signals: Iterable[Any]) -> None:
        rows = [
            {
                "id": s.id,
                "group_key": s.group_key,
                "exchange": getattr(s, "exchange", "bybit") or "bybit",
                "symbol": s.symbol,
                "signal_type": s.signal_type.value,
                "interval_sec": s.interval_sec,
                "change_pct": s.change_pct,
                "price": s.price,
                "prev_price": s.prev_price,
                "payload": s.to_payload(),
                "detected_at": s.detected_at,
            }
            for s in signals
        ]
        if not rows:
            return
        async with session_scope(self._sf) as session:
            await session.execute(insert(CommercialSignalRow).values(rows).on_conflict_do_nothing())

    async def save_user_signal_events(self, records: Iterable[UserSignalRecord]) -> None:
        rows = [
            {
                "user_id": r.user_id,
                "signal_id": r.signal_id,
                "status": r.status,
                "error_code": r.error_code,
                "attempts": r.attempts,
                "created_at": r.created_at,
                "sent_at": r.sent_at,
            }
            for r in records
        ]
        if not rows:
            return
        async with session_scope(self._sf) as session:
            await session.execute(
                insert(UserSignalEventRow)
                .values(rows)
                .on_conflict_do_nothing(constraint="uq_user_signal_events_user_signal")
            )

    async def purge_signals_before(self, before: datetime) -> int:
        async with session_scope(self._sf) as session:
            result = await session.execute(
                delete(CommercialSignalRow)
                .where(CommercialSignalRow.detected_at < before)
                .returning(CommercialSignalRow.id)
            )
            return len(result.all())

    # --- admin ------------------------------------------------------------------------

    async def admin_stats(self, now: datetime) -> dict[str, Any]:
        since = now - timedelta(days=30)
        async with session_scope(self._sf) as session:
            users = (
                await session.execute(
                    select(
                        func.count(),
                        func.count().filter(UserRow.trial_started_at.is_not(None)),
                        func.count().filter(
                            UserRow.trial_ends_at > now, UserRow.role == UserRole.USER.value
                        ),
                        func.count().filter(UserRow.status == UserStatus.BOT_BLOCKED.value),
                    ).select_from(UserRow)
                )
            ).one()
            subs = (
                await session.execute(
                    select(
                        func.count().filter(
                            SubscriptionRow.status == SubscriptionStatus.ACTIVE.value
                        ),
                        func.count().filter(
                            SubscriptionRow.status == SubscriptionStatus.CANCELED.value
                        ),
                    ).select_from(SubscriptionRow)
                )
            ).one()
            paid_users = (
                await session.execute(
                    select(func.count(func.distinct(PaymentRow.user_id))).where(
                        PaymentRow.status == PaymentStatus.APPLIED.value,
                        PaymentRow.kind != PaymentKind.REFUND.value,
                    )
                )
            ).scalar_one()
            converted = (
                await session.execute(
                    select(func.count(func.distinct(PaymentRow.user_id)))
                    .join(UserRow, UserRow.id == PaymentRow.user_id)
                    .where(
                        PaymentRow.status == PaymentStatus.APPLIED.value,
                        PaymentRow.kind != PaymentKind.REFUND.value,
                        UserRow.trial_started_at.is_not(None),
                    )
                )
            ).scalar_one()
            revenue = (
                await session.execute(
                    select(
                        func.coalesce(
                            func.sum(
                                case(
                                    (
                                        PaymentRow.kind == PaymentKind.REFUND.value,
                                        -PaymentRow.total_amount,
                                    ),
                                    else_=PaymentRow.total_amount,
                                )
                            ),
                            0,
                        )
                    ).where(
                        PaymentRow.status == PaymentStatus.APPLIED.value,
                        PaymentRow.received_at >= since,
                    )
                )
            ).scalar_one()
            anomalies = (
                await session.execute(
                    select(func.count()).where(
                        PaymentRow.status == PaymentStatus.ANOMALY.value,
                        PaymentRow.received_at >= since,
                    )
                )
            ).scalar_one()
        return {
            "users_total": int(users[0]),
            "trials_used": int(users[1]),
            "trials_active": int(users[2]),
            "bot_blocked": int(users[3]),
            "subscriptions_active": int(subs[0]),
            "subscriptions_canceled": int(subs[1]),
            "paid_users": int(paid_users),
            "trial_converted": int(converted),
            "revenue_stars_30d": int(revenue),
            "payment_anomalies_30d": int(anomalies),
        }
