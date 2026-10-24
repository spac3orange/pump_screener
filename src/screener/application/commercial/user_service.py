"""Subscriber accounts, per-user detection config and the eligible-user registry."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol
from uuid import UUID

from screener.application.clock import Clock
from screener.application.commercial.access import access_state, has_access, trial_available
from screener.application.commercial.registry import ActiveUserRegistry, UserEntry
from screener.config import (
    MONITORING_CONFIG_SCHEMA_VERSION,
    CommercialSettings,
    ScreenerSettings,
)
from screener.domain.commercial import (
    DEFAULT_TEMPLATE,
    USER_CONFIG_SCHEMA_VERSION,
    AccessState,
    SubscriptionInfo,
    UserAccount,
    UserDetectionConfig,
    UserRole,
    UserStatus,
)
from screener.domain.exchanges import ALL_EXCHANGES, normalize_exchanges

logger = logging.getLogger(__name__)


class UserStore(Protocol):
    async def upsert_user(self, **kwargs: Any) -> tuple[UserAccount, bool]: ...
    async def sync_admin_roles(self, admin_telegram_ids: Any) -> None: ...
    async def get_user(self, user_id: int) -> UserAccount | None: ...
    async def set_monitoring(self, user_id: int, enabled: bool, now: Any) -> None: ...
    async def set_exchanges(self, user_id: int, exchanges: Any, now: Any) -> None: ...
    async def accept_terms(self, user_id: int, version: str, now: Any) -> None: ...
    async def mark_bot_blocked(self, user_id: int, now: Any) -> None: ...
    async def get_template(self, code: str) -> tuple[dict[str, Any], int, str] | None: ...
    async def upsert_template(self, code: str, **kwargs: Any) -> None: ...
    async def ensure_profile(self, user_id: int, **kwargs: Any) -> bool: ...
    async def add_profile_version(self, user_id: int, **kwargs: Any) -> int: ...
    async def get_user_config(self, user_id: int) -> Any: ...
    async def load_eligible(self, now: Any) -> list[tuple[UserAccount, Any]]: ...
    async def get_live_subscription(self, user_id: int) -> SubscriptionInfo | None: ...
    async def has_paid(self, user_id: int) -> bool: ...


@dataclass(slots=True, frozen=True)
class AccountView:
    user: UserAccount
    access: AccessState
    subscription: SubscriptionInfo | None
    trial_available: bool
    config: UserDetectionConfig


class UserService:
    def __init__(
        self,
        store: UserStore,
        registry: ActiveUserRegistry,
        clock: Clock,
        settings: CommercialSettings,
    ) -> None:
        self._store = store
        self._registry = registry
        self._clock = clock
        self._settings = settings
        self._template: ScreenerSettings | None = None
        # Per Telegram user: bumped before every DB read that feeds a registry write, so a
        # snapshot read before a refund/expiry cannot overwrite the newer state after it.
        self._sync_epoch: dict[int, int] = {}

    def _bump(self, telegram_user_id: int) -> int:
        epoch = self._sync_epoch.get(telegram_user_id, 0) + 1
        self._sync_epoch[telegram_user_id] = epoch
        return epoch

    @property
    def grace(self) -> timedelta:
        return timedelta(minutes=self._settings.subscription_renewal_grace_min)

    @property
    def settings(self) -> CommercialSettings:
        return self._settings

    @property
    def template(self) -> ScreenerSettings:
        assert self._template is not None, "UserService.bootstrap() not called"
        return self._template

    def default_config(self) -> UserDetectionConfig:
        return UserDetectionConfig.from_settings(self.template)

    # --- startup -----------------------------------------------------------------

    async def bootstrap(
        self,
        seed: ScreenerSettings,
        *,
        source_profile_version_id: UUID | None,
        admin_ids: list[int],
    ) -> None:
        """Seed the subscriber template once from the admin profile, then load users.

        Later admin profile edits never change existing subscribers' settings.
        """
        stored = await self._store.get_template(DEFAULT_TEMPLATE)
        if stored is None:
            await self._store.upsert_template(
                DEFAULT_TEMPLATE,
                config=seed.canonical_config(),
                schema_version=MONITORING_CONFIG_SCHEMA_VERSION,
                config_hash=seed.config_hash(),
                source_profile_version_id=source_profile_version_id,
                now=self._clock.now(),
            )
            self._template = seed
            logger.info("Subscriber template seeded from the active monitoring profile")
        else:
            config, schema, _ = stored
            try:
                self._template = ScreenerSettings.from_profile_config(config, schema)
            except ValueError:
                # A field added to ScreenerSettings later must not stop commercial mode.
                logger.error(
                    "Stored subscriber template is unreadable; using the admin profile",
                    exc_info=True,
                )
                self._template = seed
        await self._store.sync_admin_roles(admin_ids)
        await self.reload_registry()

    async def reload_registry(self) -> int:
        rows = await self._store.load_eligible(self._clock.now())
        items = []
        for user, stored in rows:
            try:
                cfg = UserDetectionConfig.from_stored(stored.config, stored.schema_version)
            except ValueError:
                logger.warning("Skipping user id=%s: unreadable monitoring config", user.id)
                continue
            items.append(self._entry(user, cfg))
        self._registry.replace_all(items)
        logger.info(
            "Commercial registry loaded users=%s groups=%s",
            self._registry.user_count,
            self._registry.group_count,
        )
        return len(items)

    # --- accounts ----------------------------------------------------------------

    async def register(
        self,
        *,
        telegram_user_id: int,
        chat_id: int,
        language_code: str | None,
        is_admin: bool,
        username: str | None = None,
        display_name: str | None = None,
    ) -> tuple[UserAccount, bool]:
        """Called for every user update; cheap when nothing changed. Returns (user, created)."""
        role = UserRole.ADMIN if is_admin else UserRole.USER
        epoch = self._bump(telegram_user_id)
        user, created = await self._store.upsert_user(
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            language_code=language_code,
            role=role,
            now=self._clock.now(),
            username=username,
            display_name=display_name,
        )
        if created:
            cfg = self.default_config()
            await self._store.ensure_profile(
                user.id,
                config=cfg.model_dump(mode="json"),
                schema_version=USER_CONFIG_SCHEMA_VERSION,
                config_hash=cfg.to_settings(self.template).config_hash(),
            )
        await self._sync(user, epoch=epoch)
        return user, created

    async def get(self, user_id: int) -> UserAccount | None:
        return await self._store.get_user(user_id)

    async def refresh(self, user_id: int) -> UserAccount | None:
        user = await self._store.get_user(user_id)
        if user is None:
            self._registry.remove(user_id)
            return None
        await self._sync(user, force=True)
        return user

    async def apply_account(self, user: UserAccount) -> None:
        """Re-index a user after an access change (trial, payment, refund)."""
        await self._sync(user, force=True)

    async def set_monitoring(self, user: UserAccount, enabled: bool) -> UserAccount | None:
        await self._store.set_monitoring(user.id, enabled, self._clock.now())
        return await self.refresh(user.id)

    async def set_exchanges(
        self, user: UserAccount, exchanges: frozenset[str]
    ) -> UserAccount | None:
        chosen = normalize_exchanges(exchanges)
        if not chosen:
            chosen = frozenset(ALL_EXCHANGES)
        await self._store.set_exchanges(user.id, sorted(chosen), self._clock.now())
        return await self.refresh(user.id)

    async def accept_terms(self, user: UserAccount) -> UserAccount | None:
        await self._store.accept_terms(user.id, self._settings.terms_version, self._clock.now())
        return await self._store.get_user(user.id)

    async def mark_blocked(self, user_id: int) -> None:
        self._registry.remove(user_id)
        try:
            await self._store.mark_bot_blocked(user_id, self._clock.now())
        except Exception:
            logger.warning("Failed to mark user id=%s as bot_blocked", user_id, exc_info=True)

    # --- config ------------------------------------------------------------------

    async def get_config(self, user_id: int) -> UserDetectionConfig:
        stored = await self._store.get_user_config(user_id)
        if stored is None:
            return self.default_config()
        try:
            return UserDetectionConfig.from_stored(stored.config, stored.schema_version)
        except ValueError:
            return self.default_config()

    async def update_config(self, user: UserAccount, cfg: UserDetectionConfig) -> int:
        """New immutable config version; the registry picks it up immediately."""
        await self._store.ensure_profile(
            user.id,
            config=cfg.model_dump(mode="json"),
            schema_version=USER_CONFIG_SCHEMA_VERSION,
            config_hash=cfg.to_settings(self.template).config_hash(),
        )
        version = await self._store.add_profile_version(
            user.id,
            config=cfg.model_dump(mode="json"),
            schema_version=USER_CONFIG_SCHEMA_VERSION,
            config_hash=cfg.to_settings(self.template).config_hash(),
            now=self._clock.now(),
        )
        await self._sync(user, cfg=cfg, force=True)
        return version

    async def reset_config(self, user: UserAccount) -> UserDetectionConfig:
        cfg = self.default_config()
        await self.update_config(user, cfg)
        return cfg

    # --- views -------------------------------------------------------------------

    async def account_view(self, user: UserAccount) -> AccountView:
        sub = await self._store.get_live_subscription(user.id)
        paid = sub is not None or await self._store.has_paid(user.id)
        return AccountView(
            user=user,
            access=access_state(user, sub, self._clock.now(), grace=self.grace),
            subscription=sub,
            trial_available=trial_available(user, has_paid=paid),
            config=await self.get_config(user.id),
        )

    def has_access(self, user: UserAccount) -> bool:
        return has_access(user.role, user.access_until, self._clock.now())

    # --- registry sync -----------------------------------------------------------

    def _eligible(self, user: UserAccount) -> bool:
        return (
            user.status is UserStatus.ACTIVE
            and user.monitoring_enabled
            and has_access(user.role, user.access_until, self._clock.now())
        )

    def _entry(
        self, user: UserAccount, cfg: UserDetectionConfig
    ) -> tuple[UserEntry, ScreenerSettings]:
        settings = cfg.to_settings(self.template)
        entry = UserEntry(
            user_id=user.id,
            chat_id=user.chat_id,
            role=user.role,
            access_until=user.access_until,
            group_key=settings.config_hash(),
            exchanges=user.enabled_exchanges,
        )
        return entry, settings

    async def _sync(
        self,
        user: UserAccount,
        *,
        cfg: UserDetectionConfig | None = None,
        force: bool = False,
        epoch: int | None = None,
    ) -> None:
        tg_id = user.telegram_user_id
        if epoch is None:
            epoch = self._bump(tg_id)
        elif self._sync_epoch.get(tg_id) != epoch:
            return  # a newer snapshot of this user is already being applied
        current = self._registry.get(user.id)
        if not self._eligible(user):
            if current is not None:
                self._registry.remove(user.id)
            return
        if (
            not force
            and current is not None
            and current.chat_id == user.chat_id
            and current.role is user.role
            and current.access_until == user.access_until
            and current.exchanges == user.enabled_exchanges
        ):
            return
        if cfg is None:
            cfg = await self._config_for_registry(user.id)
            if self._sync_epoch.get(tg_id) != epoch:
                return
        self._registry.upsert(*self._entry(user, cfg))

    async def _config_for_registry(self, user_id: int) -> UserDetectionConfig:
        stored = await self._store.get_user_config(user_id)
        if stored is not None:
            try:
                return UserDetectionConfig.from_stored(stored.config, stored.schema_version)
            except ValueError:
                return self.default_config()
        # Profile creation failed at signup: create it now, or a restart drops the user.
        cfg = self.default_config()
        await self._store.ensure_profile(
            user_id,
            config=cfg.model_dump(mode="json"),
            schema_version=USER_CONFIG_SCHEMA_VERSION,
            config_hash=cfg.to_settings(self.template).config_hash(),
        )
        return cfg
