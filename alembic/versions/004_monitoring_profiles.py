"""monitoring profiles with immutable versions

Revision ID: 004
Revises: 003
Create Date: 2026-10-14

The ``Default`` profile is seeded by the application on first start from the
effective pre-profile settings (env defaults + ``screener_settings`` row), the
same merge the app used before, so detection parameters do not change.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _ts(name: str) -> sa.Column:
    return sa.Column(
        name, sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "monitoring_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("current_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_monitoring_profiles_name_ci",
        "monitoring_profiles",
        [sa.text("lower(name)")],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    op.create_table(
        "monitoring_profile_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("profile_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column("config_schema_version", sa.Integer(), nullable=False),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        _ts("created_at"),
        sa.Column("created_by_chat_id", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["profile_id"], ["monitoring_profiles.id"]),
    )
    op.create_index(
        "uq_monitoring_profile_versions_no",
        "monitoring_profile_versions",
        ["profile_id", "version_no"],
        unique=True,
    )
    op.create_foreign_key(
        "fk_monitoring_profiles_current_version",
        "monitoring_profiles",
        "monitoring_profile_versions",
        ["current_version_id"],
        ["id"],
    )

    op.create_table(
        "monitoring_runtime_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("active_profile_id", postgresql.UUID(as_uuid=True), nullable=False),
        _ts("updated_at"),
        sa.Column("updated_by_chat_id", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["active_profile_id"], ["monitoring_profiles.id"]),
        sa.CheckConstraint("id = 1", name="ck_monitoring_runtime_state_single_row"),
    )

    op.add_column(
        "signals",
        sa.Column("monitoring_profile_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "signals",
        sa.Column("monitoring_profile_version_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_signals_monitoring_profile_version",
        "signals",
        "monitoring_profile_versions",
        ["monitoring_profile_version_id"],
        ["id"],
    )
    op.create_index("ix_signals_profile_version", "signals", ["monitoring_profile_version_id"])


def downgrade() -> None:
    op.drop_index("ix_signals_profile_version", table_name="signals")
    op.drop_constraint("fk_signals_monitoring_profile_version", "signals", type_="foreignkey")
    op.drop_column("signals", "monitoring_profile_version_id")
    op.drop_column("signals", "monitoring_profile_id")
    op.drop_table("monitoring_runtime_state")
    op.drop_constraint(
        "fk_monitoring_profiles_current_version", "monitoring_profiles", type_="foreignkey"
    )
    op.drop_index("uq_monitoring_profile_versions_no", table_name="monitoring_profile_versions")
    op.drop_table("monitoring_profile_versions")
    op.drop_index("uq_monitoring_profiles_name_ci", table_name="monitoring_profiles")
    op.drop_table("monitoring_profiles")
