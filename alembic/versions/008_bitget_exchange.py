"""Append bitget to users.enabled_exchanges.

Revision ID: 008
Revises: 007
Create Date: 2026-10-17

Data-only. New rows default to ``{bybit,binance,bitget}``. Existing rows gain
``bitget`` when it is missing. Downgrade removes only that element.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "008"
down_revision: str | None = "007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_WITH_BITGET = "ARRAY['bybit','binance','bitget']::text[]"
_WITHOUT_BITGET = "ARRAY['bybit','binance']::text[]"


def upgrade() -> None:
    op.execute(
        "ALTER TABLE users ALTER COLUMN enabled_exchanges "
        f"SET DEFAULT {_WITH_BITGET}"
    )
    op.execute(
        "UPDATE users SET enabled_exchanges = enabled_exchanges || ARRAY['bitget']::text[] "
        "WHERE NOT ('bitget' = ANY(enabled_exchanges))"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE users SET enabled_exchanges = array_remove(enabled_exchanges, 'bitget')"
    )
    op.execute(
        "ALTER TABLE users ALTER COLUMN enabled_exchanges "
        f"SET DEFAULT {_WITHOUT_BITGET}"
    )
