from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from screener.domain.profiles import (
    MonitoringProfile,
    ProfileNameTaken,
    ProfileNotFound,
    ProfileProtected,
    ProfileVersion,
)
from screener.infrastructure.db.models import (
    MonitoringProfileRow,
    MonitoringProfileVersionRow,
    MonitoringRuntimeStateRow,
)
from screener.infrastructure.db.session import session_scope

_NAME_INDEX = "uq_monitoring_profiles_name_ci"


def _version(row: MonitoringProfileVersionRow) -> ProfileVersion:
    return ProfileVersion(
        id=row.id,
        profile_id=row.profile_id,
        version_no=row.version_no,
        config=dict(row.config),
        config_schema_version=row.config_schema_version,
        config_hash=row.config_hash,
        created_at=row.created_at,
    )


def _profile(row: MonitoringProfileRow, version: MonitoringProfileVersionRow) -> MonitoringProfile:
    return MonitoringProfile(
        id=row.id,
        name=row.name,
        description=row.description,
        is_system=row.is_system,
        current=_version(version),
    )


def _name_conflict(exc: IntegrityError) -> bool:
    return _NAME_INDEX in str(exc.orig)


class ProfileRepository:
    """PostgreSQL store for monitoring profiles. Versions are append-only."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def list_profiles(self) -> list[MonitoringProfile]:
        async with session_scope(self._sf) as session:
            result = await session.execute(
                select(MonitoringProfileRow, MonitoringProfileVersionRow)
                .join(
                    MonitoringProfileVersionRow,
                    MonitoringProfileVersionRow.id == MonitoringProfileRow.current_version_id,
                )
                .where(MonitoringProfileRow.deleted_at.is_(None))
                .order_by(MonitoringProfileRow.created_at, MonitoringProfileRow.name)
            )
            return [_profile(p, v) for p, v in result.all()]

    async def get_profile(self, profile_id: UUID) -> MonitoringProfile | None:
        async with session_scope(self._sf) as session:
            return await self._get(session, profile_id)

    async def _get(self, session: AsyncSession, profile_id: UUID) -> MonitoringProfile | None:
        row = await session.get(MonitoringProfileRow, profile_id)
        if row is None or row.deleted_at is not None or row.current_version_id is None:
            return None
        version = await session.get(MonitoringProfileVersionRow, row.current_version_id)
        assert version is not None
        return _profile(row, version)

    async def get_active_profile_id(self) -> UUID | None:
        async with session_scope(self._sf) as session:
            row = await session.get(MonitoringRuntimeStateRow, 1)
            return None if row is None else row.active_profile_id

    async def create_profile(
        self,
        *,
        name: str,
        description: str | None,
        config: dict[str, Any],
        config_schema_version: int,
        config_hash: str,
        chat_id: int | None,
        is_system: bool = False,
    ) -> MonitoringProfile:
        profile_id, version_id = uuid4(), uuid4()
        now = datetime.now(UTC)
        try:
            async with session_scope(self._sf) as session:
                profile = MonitoringProfileRow(
                    id=profile_id,
                    name=name,
                    description=description,
                    is_system=is_system,
                    created_at=now,
                    updated_at=now,
                )
                session.add(profile)
                await session.flush()
                version = MonitoringProfileVersionRow(
                    id=version_id,
                    profile_id=profile_id,
                    version_no=1,
                    config=config,
                    config_schema_version=config_schema_version,
                    config_hash=config_hash,
                    created_at=now,
                    created_by_chat_id=chat_id,
                )
                session.add(version)
                await session.flush()
                profile.current_version_id = version_id
                await session.flush()
                return _profile(profile, version)
        except IntegrityError as exc:
            if _name_conflict(exc):
                raise ProfileNameTaken(f"Профиль «{name}» уже существует.") from None
            raise

    async def add_version(
        self,
        profile_id: UUID,
        *,
        config: dict[str, Any],
        config_schema_version: int,
        config_hash: str,
        chat_id: int | None,
    ) -> MonitoringProfile:
        async with session_scope(self._sf) as session:
            profile = await session.get(MonitoringProfileRow, profile_id, with_for_update=True)
            if profile is None or profile.deleted_at is not None:
                raise ProfileNotFound("Профиль не найден.")
            last = await session.scalar(
                select(func.max(MonitoringProfileVersionRow.version_no)).where(
                    MonitoringProfileVersionRow.profile_id == profile_id
                )
            )
            now = datetime.now(UTC)
            version = MonitoringProfileVersionRow(
                id=uuid4(),
                profile_id=profile_id,
                version_no=(last or 0) + 1,
                config=config,
                config_schema_version=config_schema_version,
                config_hash=config_hash,
                created_at=now,
                created_by_chat_id=chat_id,
            )
            session.add(version)
            await session.flush()
            profile.current_version_id = version.id
            profile.updated_at = now
            await session.flush()
            return _profile(profile, version)

    async def rename(self, profile_id: UUID, name: str) -> None:
        try:
            async with session_scope(self._sf) as session:
                result = await session.execute(
                    update(MonitoringProfileRow)
                    .where(
                        MonitoringProfileRow.id == profile_id,
                        MonitoringProfileRow.deleted_at.is_(None),
                    )
                    .values(name=name, updated_at=datetime.now(UTC))
                )
                if result.rowcount == 0:
                    raise ProfileNotFound("Профиль не найден.")
        except IntegrityError as exc:
            if _name_conflict(exc):
                raise ProfileNameTaken(f"Профиль «{name}» уже существует.") from None
            raise

    async def soft_delete(self, profile_id: UUID) -> None:
        """Hide a profile; its versions stay for signal provenance."""
        async with session_scope(self._sf) as session:
            state = await session.get(MonitoringRuntimeStateRow, 1, with_for_update=True)
            if state is not None and state.active_profile_id == profile_id:
                raise ProfileProtected("Нельзя удалить активный профиль.")
            await session.execute(
                update(MonitoringProfileRow)
                .where(MonitoringProfileRow.id == profile_id)
                .values(deleted_at=datetime.now(UTC))
            )

    async def set_active(self, profile_id: UUID, *, chat_id: int | None) -> None:
        async with session_scope(self._sf) as session:
            now = datetime.now(UTC)
            stmt = insert(MonitoringRuntimeStateRow).values(
                id=1, active_profile_id=profile_id, updated_at=now, updated_by_chat_id=chat_id
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[MonitoringRuntimeStateRow.id],
                set_={
                    "active_profile_id": profile_id,
                    "updated_at": now,
                    "updated_by_chat_id": chat_id,
                },
            )
            await session.execute(stmt)

    async def get_version(self, version_id: UUID) -> ProfileVersion | None:
        async with session_scope(self._sf) as session:
            row = await session.get(MonitoringProfileVersionRow, version_id)
            return None if row is None else _version(row)
