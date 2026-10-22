"""Store Telegram username and display name for users who started the bot.

Revision ID: 009
Revises: 008
Create Date: 2026-10-22

Additive. Existing rows keep null names until the user writes to the bot again.
Downgrade drops only these two columns.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "009"
down_revision: str | None = "008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("username", sa.String(length=32), nullable=True))
    op.add_column("users", sa.Column("display_name", sa.String(length=128), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "display_name")
    op.drop_column("users", "username")
