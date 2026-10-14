"""position lifecycle: close tracking on trading_executions

Revision ID: 005
Revises: 004
Create Date: 2026-10-14
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns() -> list[sa.Column]:
    money = sa.Numeric(38, 12)
    return [
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("remaining_qty", money, nullable=True),
        sa.Column("tp_order_id", sa.String(length=64), nullable=True),
        sa.Column("sl_order_id", sa.String(length=64), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("close_reason", sa.String(length=32), nullable=True),
        sa.Column("close_reason_detail", sa.String(length=200), nullable=True),
        sa.Column("exit_price", money, nullable=True),
        sa.Column("closed_qty", money, nullable=True),
        sa.Column("realized_pnl", money, nullable=True),
        sa.Column("trading_fee", money, nullable=True),
        sa.Column("funding_fee", money, nullable=True),
        sa.Column("net_pnl", money, nullable=True),
        sa.Column("bybit_closed_pnl", money, nullable=True),
        sa.Column("balance_after", money, nullable=True),
        sa.Column("close_order_id", sa.String(length=64), nullable=True),
        sa.Column("close_execution_id", sa.String(length=64), nullable=True),
        sa.Column(
            "close_recovered", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("close_notified_at", sa.DateTime(timezone=True), nullable=True),
    ]


def upgrade() -> None:
    for column in _columns():
        op.add_column("trading_executions", column)
    # Existing filled trades: the reservation time is the best known lower bound of entry.
    op.execute(
        "UPDATE trading_executions SET opened_at = created_at "
        "WHERE status IN ('FILLED', 'PARTIALLY_FILLED', 'PROTECTIVE_FAILED')"
    )
    op.create_index(
        "ix_trading_executions_closed_unnotified",
        "trading_executions",
        ["closed_at"],
        postgresql_where=sa.text("status = 'CLOSED' AND close_notified_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_trading_executions_closed_unnotified", table_name="trading_executions")
    for column in reversed(_columns()):
        op.drop_column("trading_executions", column.name)
