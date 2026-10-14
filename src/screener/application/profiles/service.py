from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from screener.config import MONITORING_CONFIG_SCHEMA_VERSION, ScreenerSettings
from screener.domain.models import SignalContext
from screener.domain.profiles import (
    DEFAULT_PROFILE_NAME,
    MonitoringProfile,
    ProfileNotFound,
    ProfileProtected,
    ProfileValidationError,
    ProfileVersion,
    normalize_description,
    normalize_profile_name,
)
from screener.logging import log

logger = logging.getLogger(__name__)


class ProfileStore(Protocol):
    async def list_profiles(self) -> list[MonitoringProfile]: ...

    async def get_profile(self, profile_id: UUID) -> MonitoringProfile | None: ...

    async def get_active_profile_id(self) -> UUID | None: ...

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
    ) -> MonitoringProfile: ...

    async def add_version(
        self,
        profile_id: UUID,
        *,
        config: dict[str, Any],
        config_schema_version: int,
        config_hash: str,
        chat_id: int | None,
    ) -> MonitoringProfile: ...

    async def rename(self, profile_id: UUID, name: str) -> None: ...

    async def soft_delete(self, profile_id: UUID) -> None: ...

    async def set_active(self, profile_id: UUID, *, chat_id: int | None) -> None: ...


@dataclass(slots=True, frozen=True)
class ActiveProfile:
    """Validated runtime monitoring config plus the profile version it came from."""

    settings: ScreenerSettings
    profile: MonitoringProfile | None

    @property
    def name(self) -> str:
        return self.profile.name if self.profile else "(env defaults)"

    @property
    def context(self) -> SignalContext:
        rule_version = self.settings.rule_version()
        if self.profile is None:
            return SignalContext(rule_version=rule_version)
        return SignalContext(
            rule_version=rule_version,
            profile_id=self.profile.id,
            profile_version_id=self.profile.current.id,
            profile_name=self.profile.name,
            profile_version_no=self.profile.current.version_no,
        )


ApplyFunc = Callable[[ActiveProfile, bool], None]
EventLogFunc = Callable[..., Awaitable[None]]
LegacyLoader = Callable[[], Awaitable[ScreenerSettings]]


def settings_from_version(version: ProfileVersion) -> ScreenerSettings:
    try:
        return ScreenerSettings.from_profile_config(version.config, version.config_schema_version)
    except ValueError as exc:
        raise ProfileValidationError(f"Профиль содержит некорректные настройки: {exc}") from None


class ProfileService:
    """Monitoring profiles: PostgreSQL is the source of truth, ``.env`` only seeds ``Default``.

    Every mutation persists first and only then swaps the in-memory snapshot via
    ``apply``, so a failed write or invalid config never replaces the active one.
    """

    def __init__(
        self,
        store: ProfileStore,
        *,
        apply: ApplyFunc | None = None,
        event_log: EventLogFunc | None = None,
    ) -> None:
        self._store = store
        self._apply = apply
        self._event_log = event_log
        self._lock = asyncio.Lock()
        self._active: ActiveProfile | None = None

    @property
    def active(self) -> ActiveProfile:
        if self._active is None:
            raise RuntimeError("ProfileService.bootstrap() was not called")
        return self._active

    def set_apply(self, apply: ApplyFunc) -> None:
        self._apply = apply

    def _set_active(self, active: ActiveProfile, *, reset_dedup: bool) -> None:
        self._active = active
        if self._apply is not None:
            self._apply(active, reset_dedup)

    async def bootstrap(self, legacy_loader: LegacyLoader) -> ActiveProfile:
        async with self._lock:
            profiles = await self._store.list_profiles()
            if not profiles:
                active = await self._migrate_legacy(legacy_loader)
            else:
                active = await self._load_active(profiles, legacy_loader)
            self._set_active(active, reset_dedup=False)
            log(
                logger,
                logging.INFO,
                f"Monitoring profile active: {active.name} "
                f"v{active.profile.current.version_no if active.profile else '-'} "
                f"hash={active.settings.rule_version()}",
                component="profiles",
                event="profile_bootstrap",
            )
            return active

    async def _migrate_legacy(self, legacy_loader: LegacyLoader) -> ActiveProfile:
        settings = await legacy_loader()
        config = settings.canonical_config()
        profile = await self._store.create_profile(
            name=DEFAULT_PROFILE_NAME,
            description="Перенесено из прежних настроек",
            config=config,
            config_schema_version=MONITORING_CONFIG_SCHEMA_VERSION,
            config_hash=settings.config_hash(),
            chat_id=None,
            is_system=True,
        )
        await self._store.set_active(profile.id, chat_id=None)
        await self._log_event(
            "monitoring_profiles_migrated",
            f"Seeded {DEFAULT_PROFILE_NAME} from previous settings",
            {"profile_id": str(profile.id), "config_hash": settings.config_hash()},
        )
        return ActiveProfile(settings=settings_from_version(profile.current), profile=profile)

    async def _load_active(
        self, profiles: list[MonitoringProfile], legacy_loader: LegacyLoader
    ) -> ActiveProfile:
        active_id = await self._store.get_active_profile_id()
        ordered = sorted(
            profiles, key=lambda p: (p.id != active_id, not p.is_system, p.name.lower())
        )
        for profile in ordered:
            try:
                settings = settings_from_version(profile.current)
            except ProfileValidationError:
                logger.error("Monitoring profile %s has invalid config; skipping", profile.name)
                continue
            if profile.id != active_id:
                logger.warning("Active monitoring profile unavailable; using %s", profile.name)
                await self._store.set_active(profile.id, chat_id=None)
            return ActiveProfile(settings=settings, profile=profile)
        logger.critical("No valid monitoring profile in DB; running on env defaults")
        return ActiveProfile(settings=await legacy_loader(), profile=None)

    async def list_profiles(self) -> list[MonitoringProfile]:
        return await self._store.list_profiles()

    async def get_profile(self, profile_id: UUID) -> MonitoringProfile:
        profile = await self._store.get_profile(profile_id)
        if profile is None:
            raise ProfileNotFound("Профиль не найден.")
        return profile

    async def create_from_current(
        self, name: str, *, description: str | None = None, chat_id: int | None = None
    ) -> MonitoringProfile:
        return await self._create(name, description, self.active.settings, chat_id)

    async def clone(
        self, source_id: UUID, name: str, *, chat_id: int | None = None
    ) -> MonitoringProfile:
        source = await self.get_profile(source_id)
        settings = settings_from_version(source.current)
        return await self._create(name, f"Копия «{source.name}»", settings, chat_id)

    async def _create(
        self,
        name: str,
        description: str | None,
        settings: ScreenerSettings,
        chat_id: int | None,
    ) -> MonitoringProfile:
        clean = normalize_profile_name(name)
        desc = normalize_description(description)
        async with self._lock:
            profile = await self._store.create_profile(
                name=clean,
                description=desc,
                config=settings.canonical_config(),
                config_schema_version=MONITORING_CONFIG_SCHEMA_VERSION,
                config_hash=settings.config_hash(),
                chat_id=chat_id,
            )
        await self._log_event(
            "monitoring_profile_created",
            f"Created profile {clean}",
            {"profile_id": str(profile.id), "chat_id": chat_id},
        )
        return profile

    async def rename(self, profile_id: UUID, name: str) -> MonitoringProfile:
        clean = normalize_profile_name(name)
        async with self._lock:
            await self._store.rename(profile_id, clean)
            profile = await self.get_profile(profile_id)
            active = self.active
            if active.profile is not None and active.profile.id == profile_id:
                self._set_active(
                    ActiveProfile(settings=active.settings, profile=profile), reset_dedup=False
                )
        return profile

    async def delete(self, profile_id: UUID) -> None:
        async with self._lock:
            profile = await self.get_profile(profile_id)
            if profile.is_system:
                raise ProfileProtected("Системный профиль нельзя удалить.")
            active = self.active
            if active.profile is not None and active.profile.id == profile_id:
                raise ProfileProtected(
                    "Нельзя удалить активный профиль. Сначала активируйте другой."
                )
            await self._store.soft_delete(profile_id)
        await self._log_event(
            "monitoring_profile_deleted",
            f"Deleted profile {profile.name}",
            {"profile_id": str(profile_id)},
        )

    async def activate(self, profile_id: UUID, *, chat_id: int | None = None) -> ActiveProfile:
        async with self._lock:
            profile = await self.get_profile(profile_id)
            settings = settings_from_version(profile.current)
            previous = self.active
            await self._store.set_active(profile.id, chat_id=chat_id)
            active = ActiveProfile(settings=settings, profile=profile)
            # Config-dependent dedup state is reset; price history, WS and registry are kept.
            self._set_active(active, reset_dedup=True)
        log(
            logger,
            logging.INFO,
            f"Monitoring profile changed {previous.name} -> {active.name}",
            component="profiles",
            event="monitoring_profile_changed",
        )
        await self._log_event(
            "monitoring_profile_changed",
            f"{previous.name} -> {active.name}",
            {
                "from_profile_id": _id(previous.profile),
                "to_profile_id": str(profile.id),
                "to_version_id": str(profile.current.id),
                "config_hash": settings.config_hash(),
                "chat_id": chat_id,
            },
        )
        return active

    async def update_active_settings(
        self, settings: ScreenerSettings, *, chat_id: int | None = None
    ) -> ActiveProfile:
        """``/settings`` edits: append an immutable version to the active profile."""
        config = settings.canonical_config()
        try:
            validated = ScreenerSettings.from_profile_config(
                config, MONITORING_CONFIG_SCHEMA_VERSION
            )
        except ValueError as exc:
            raise ProfileValidationError(str(exc)) from None
        async with self._lock:
            current = self.active
            if current.profile is None:
                raise ProfileNotFound("Нет активного профиля в БД.")
            if validated.config_hash() == current.settings.config_hash():
                return current
            profile = await self._store.add_version(
                current.profile.id,
                config=config,
                config_schema_version=MONITORING_CONFIG_SCHEMA_VERSION,
                config_hash=validated.config_hash(),
                chat_id=chat_id,
            )
            active = ActiveProfile(settings=validated, profile=profile)
            self._set_active(active, reset_dedup=False)
            return active

    async def _log_event(self, event_type: str, message: str, details: dict) -> None:
        if self._event_log is None:
            return
        try:
            await self._event_log(
                level="INFO",
                component="profiles",
                event_type=event_type,
                message=message,
                details=details,
            )
        except Exception:
            logger.warning("Failed to log %s", event_type, exc_info=True)


def _id(profile: MonitoringProfile | None) -> str | None:
    return None if profile is None else str(profile.id)
