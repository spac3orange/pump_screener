"""commercial MVP: users, user profiles, Stars invoices/subscriptions/payments, user signals

Revision ID: 006
Revises: 005
Create Date: 2026-10-15

Additive only: no existing table is altered, so v3.1 keeps working on this schema.
The ``default`` user profile template is seeded by the application on first
start from the active admin monitoring profile (same approach as 004).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _ts(name: str) -> sa.Column:
    return sa.Column(
        name, sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def _opt_ts(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=True)


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False, server_default="user"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("language_code", sa.String(8), nullable=True),
        sa.Column(
            "monitoring_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("terms_version", sa.String(16), nullable=True),
        _opt_ts("terms_accepted_at"),
        _opt_ts("trial_started_at"),
        _opt_ts("trial_ends_at"),
        _opt_ts("access_until"),
        _opt_ts("expiry_notified_until"),
        _opt_ts("bot_blocked_at"),
        _ts("created_at"),
        _ts("updated_at"),
        _ts("last_seen_at"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("telegram_user_id", name="uq_users_telegram_user_id"),
        sa.CheckConstraint("role IN ('user','admin')", name="ck_users_role"),
        sa.CheckConstraint(
            "status IN ('active','bot_blocked','banned')", name="ck_users_status"
        ),
        sa.CheckConstraint(
            "(trial_started_at IS NULL) = (trial_ends_at IS NULL)", name="ck_users_trial_pair"
        ),
    )
    op.create_index(
        "ix_users_access_until",
        "users",
        ["access_until"],
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_users_expiry_pending",
        "users",
        ["access_until"],
        postgresql_where=sa.text(
            "access_until IS NOT NULL AND "
            "(expiry_notified_until IS NULL OR expiry_notified_until < access_until)"
        ),
    )

    op.create_table(
        "user_profile_templates",
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column("config_schema_version", sa.Integer(), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("source_profile_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        _ts("updated_at"),
        sa.PrimaryKeyConstraint("code"),
        sa.ForeignKeyConstraint(
            ["source_profile_version_id"], ["monitoring_profile_versions.id"]
        ),
    )

    op.create_table(
        "user_monitoring_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("current_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", name="uq_user_monitoring_profiles_user"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "user_monitoring_profile_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("profile_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column("config_schema_version", sa.Integer(), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["profile_id"], ["user_monitoring_profiles.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("profile_id", "version_no", name="uq_user_profile_versions_no"),
    )
    op.create_index(
        "ix_user_profile_versions_hash", "user_monitoring_profile_versions", ["config_hash"]
    )
    op.create_foreign_key(
        "fk_user_monitoring_profiles_current_version",
        "user_monitoring_profiles",
        "user_monitoring_profile_versions",
        ["current_version_id"],
        ["id"],
    )

    op.create_table(
        "payment_invoices",
        sa.Column("payload", sa.String(64), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("plan_code", sa.String(32), nullable=False),
        sa.Column("amount_stars", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="created"),
        _ts("created_at"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        _opt_ts("paid_at"),
        sa.PrimaryKeyConstraint("payload"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.CheckConstraint("currency = 'XTR'", name="ck_payment_invoices_currency"),
        sa.CheckConstraint("amount_stars > 0", name="ck_payment_invoices_amount"),
        sa.CheckConstraint(
            "status IN ('created','pre_checkout_ok','paid','expired')",
            name="ck_payment_invoices_status",
        ),
    )
    op.create_index(
        "ix_payment_invoices_user_created", "payment_invoices", ["user_id", "created_at"]
    )

    op.create_table(
        "subscriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("plan_code", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("invoice_payload", sa.String(64), nullable=False),
        sa.Column("subscription_charge_id", sa.String(128), nullable=False),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "cancel_at_period_end", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        _ts("started_at"),
        _opt_ts("canceled_at"),
        _opt_ts("ended_at"),
        _ts("created_at"),
        _ts("updated_at"),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.UniqueConstraint("invoice_payload", name="uq_subscriptions_invoice_payload"),
        sa.UniqueConstraint("subscription_charge_id", name="uq_subscriptions_charge_id"),
        sa.CheckConstraint(
            "status IN ('active','canceled','expired','refunded')",
            name="ck_subscriptions_status",
        ),
    )
    op.create_index(
        "uq_subscriptions_one_live_per_user",
        "subscriptions",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('active','canceled')"),
    )

    op.create_table(
        "payments",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("subscription_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("invoice_payload", sa.String(64), nullable=False),
        sa.Column("telegram_payment_charge_id", sa.String(128), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("total_amount", sa.Integer(), nullable=False),
        sa.Column("is_recurring", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "is_first_recurring", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        _opt_ts("subscription_expiration_date"),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("anomaly_reason", sa.String(64), nullable=True),
        _ts("received_at"),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["subscription_id"], ["subscriptions.id"]),
        sa.UniqueConstraint(
            "telegram_payment_charge_id", "kind", name="uq_payments_charge_kind"
        ),
        sa.CheckConstraint(
            "kind IN ('initial','renewal','refund')", name="ck_payments_kind"
        ),
        sa.CheckConstraint("status IN ('applied','anomaly')", name="ck_payments_status"),
    )
    op.create_index("ix_payments_user_received", "payments", ["user_id", "received_at"])

    op.create_table(
        "commercial_signals",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("group_key", sa.String(64), nullable=False),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("signal_type", sa.String(32), nullable=False),
        sa.Column("interval_sec", sa.Integer(), nullable=False),
        sa.Column("change_pct", sa.Float(), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("prev_price", sa.Float(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_commercial_signals_detected_at", "commercial_signals", ["detected_at"])

    op.create_table(
        "user_signal_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("signal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("error_code", sa.String(32), nullable=True),
        sa.Column("attempts", sa.SmallInteger(), nullable=False, server_default="0"),
        _ts("created_at"),
        _opt_ts("sent_at"),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["signal_id"], ["commercial_signals.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("user_id", "signal_id", name="uq_user_signal_events_user_signal"),
        sa.CheckConstraint(
            "status IN ('sent','failed','dropped_expired','dropped_access',"
            "'dropped_overflow','dropped_blocked')",
            name="ck_user_signal_events_status",
        ),
    )
    op.create_index(
        "ix_user_signal_events_user_created",
        "user_signal_events",
        ["user_id", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_user_signal_events_user_created", table_name="user_signal_events")
    op.drop_table("user_signal_events")
    op.drop_index("ix_commercial_signals_detected_at", table_name="commercial_signals")
    op.drop_table("commercial_signals")
    op.drop_index("ix_payments_user_received", table_name="payments")
    op.drop_table("payments")
    op.drop_index("uq_subscriptions_one_live_per_user", table_name="subscriptions")
    op.drop_table("subscriptions")
    op.drop_index("ix_payment_invoices_user_created", table_name="payment_invoices")
    op.drop_table("payment_invoices")
    op.drop_constraint(
        "fk_user_monitoring_profiles_current_version",
        "user_monitoring_profiles",
        type_="foreignkey",
    )
    op.drop_index("ix_user_profile_versions_hash", table_name="user_monitoring_profile_versions")
    op.drop_table("user_monitoring_profile_versions")
    op.drop_table("user_monitoring_profiles")
    op.drop_table("user_profile_templates")
    op.drop_index("ix_users_expiry_pending", table_name="users")
    op.drop_index("ix_users_access_until", table_name="users")
    op.drop_table("users")
