"""initial schema

Revision ID: 001
Revises:
Create Date: 2026-10-10
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "instruments",
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("contract_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("settle_coin", sa.String(length=16), nullable=False),
        sa.Column("base_coin", sa.String(length=32), nullable=False),
        sa.Column("quote_coin", sa.String(length=32), nullable=False),
        sa.Column("symbol_type", sa.String(length=32), nullable=True),
        sa.Column("is_pre_listing", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("symbol"),
    )
    op.create_index("ix_instruments_status", "instruments", ["status"])
    op.create_index("ix_instruments_updated_at", "instruments", ["updated_at"])

    op.create_table(
        "signals",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("signal_type", sa.String(length=32), nullable=False),
        sa.Column("interval_sec", sa.Integer(), nullable=False),
        sa.Column("change_pct", sa.Float(), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("prev_price", sa.Float(), nullable=False),
        sa.Column("turnover_24h", sa.Float(), nullable=True),
        sa.Column("volume_ratio", sa.Float(), nullable=True),
        sa.Column("rule_version", sa.String(length=32), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_signals_detected_at", "signals", ["detected_at"])
    op.create_index(
        "ix_signals_symbol_type_detected",
        "signals",
        ["symbol", "signal_type", "detected_at"],
    )
    op.create_index("ix_signals_type_detected", "signals", ["signal_type", "detected_at"])

    op.create_table(
        "screener_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_by_chat_id", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "notification_history",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("signal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["signal_id"], ["signals.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_notification_signal_id", "notification_history", ["signal_id"])
    op.create_index("ix_notification_status_sent", "notification_history", ["status", "sent_at"])

    op.create_table(
        "system_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("level", sa.String(length=16), nullable=False),
        sa.Column("component", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_system_events_created_at", "system_events", ["created_at"])
    op.create_index(
        "ix_system_events_component_created",
        "system_events",
        ["component", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("system_events")
    op.drop_table("notification_history")
    op.drop_table("screener_settings")
    op.drop_table("signals")
    op.drop_table("instruments")
