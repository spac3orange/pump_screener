"""trading executions and runtime state

Revision ID: 003
Revises: 002
Create Date: 2026-10-13
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "trading_executions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("signal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("signal_type", sa.String(length=32), nullable=False),
        sa.Column("configured_action", sa.String(length=16), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=True),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("order_link_id", sa.String(length=64), nullable=False),
        sa.Column("bybit_order_id", sa.String(length=64), nullable=True),
        sa.Column("requested_qty", sa.Float(), nullable=True),
        sa.Column("filled_qty", sa.Float(), nullable=True),
        sa.Column("avg_fill_price", sa.Float(), nullable=True),
        sa.Column("leverage", sa.Float(), nullable=True),
        sa.Column("take_profit", sa.Float(), nullable=True),
        sa.Column("stop_loss", sa.Float(), nullable=True),
        sa.Column("skip_reason", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("signal_id"),
        sa.UniqueConstraint("order_link_id"),
    )
    op.create_index(
        "ix_trading_executions_symbol_created",
        "trading_executions",
        ["symbol", "created_at"],
    )
    op.create_index(
        "ix_trading_executions_status_created",
        "trading_executions",
        ["status", "created_at"],
    )

    op.create_table(
        "trading_runtime_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_by_chat_id", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        "INSERT INTO trading_runtime_state (id, enabled) VALUES (1, false) "
        "ON CONFLICT (id) DO NOTHING"
    )


def downgrade() -> None:
    op.drop_table("trading_runtime_state")
    op.drop_index("ix_trading_executions_status_created", table_name="trading_executions")
    op.drop_index("ix_trading_executions_symbol_created", table_name="trading_executions")
    op.drop_table("trading_executions")
