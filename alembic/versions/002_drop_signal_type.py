"""rename SHORT signal type to DROP (market event, not a trading side)

Revision ID: 002
Revises: 001
Create Date: 2026-10-13
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("UPDATE signals SET signal_type = 'DROP' WHERE signal_type = 'SHORT'")


def downgrade() -> None:
    op.execute("UPDATE signals SET signal_type = 'SHORT' WHERE signal_type = 'DROP'")
