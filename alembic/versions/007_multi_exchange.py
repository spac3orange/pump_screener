"""Multi-exchange monitoring: exchange columns, user exchange toggles, app_settings.

Revision ID: 007
Revises: 006
Create Date: 2026-10-17

Additive for existing rows (server defaults ``bybit``). Downgrade to 006 drops
only what this revision adds; existing Bybit data is kept.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "instruments",
        sa.Column("exchange", sa.String(length=16), server_default="bybit", nullable=False),
    )
    op.drop_constraint("instruments_pkey", "instruments", type_="primary")
    op.create_primary_key("instruments_pkey", "instruments", ["exchange", "symbol"])

    op.add_column(
        "signals",
        sa.Column("exchange", sa.String(length=16), server_default="bybit", nullable=False),
    )
    op.create_index(
        "ix_signals_exchange_symbol_detected",
        "signals",
        ["exchange", "symbol", "detected_at"],
    )

    op.add_column(
        "commercial_signals",
        sa.Column("exchange", sa.String(length=16), server_default="bybit", nullable=False),
    )
    op.create_index(
        "ix_commercial_signals_exchange_symbol_detected",
        "commercial_signals",
        ["exchange", "symbol", "detected_at"],
    )

    op.add_column(
        "users",
        sa.Column(
            "enabled_exchanges",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("ARRAY['bybit','binance']::text[]"),
            nullable=False,
        ),
    )

    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(length=64), primary_key=True),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("app_settings")
    op.drop_column("users", "enabled_exchanges")
    op.drop_index(
        "ix_commercial_signals_exchange_symbol_detected", table_name="commercial_signals"
    )
    op.drop_column("commercial_signals", "exchange")
    op.drop_index("ix_signals_exchange_symbol_detected", table_name="signals")
    op.drop_column("signals", "exchange")
    op.drop_constraint("instruments_pkey", "instruments", type_="primary")
    op.drop_column("instruments", "exchange")
    op.create_primary_key("instruments_pkey", "instruments", ["symbol"])
