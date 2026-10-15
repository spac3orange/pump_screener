"""In-memory index of users who may receive signals right now.

Only eligible users (access active, monitoring on, bot not blocked) are held.
Membership changes apply immediately (fan-out reads them); the engine's group
index is recompiled after a short debounce and swapped in one assignment.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime

from screener.application.commercial.access import has_access
from screener.application.metrics import metrics
from screener.config import ScreenerSettings
from screener.domain.commercial import UserRole

GroupsListener = Callable[[dict[str, ScreenerSettings]], None]


@dataclass(slots=True, frozen=True)
class UserEntry:
    user_id: int
    chat_id: int
    role: UserRole
    access_until: datetime | None
    group_key: str


class ActiveUserRegistry:
    def __init__(
        self, *, on_groups_changed: GroupsListener | None = None, debounce_sec: float = 0.25
    ) -> None:
        self.on_groups_changed = on_groups_changed
        self._debounce = debounce_sec
        self._users: dict[int, UserEntry] = {}
        self._members: dict[str, set[int]] = {}
        self._settings: dict[str, ScreenerSettings] = {}
        self._handle: asyncio.TimerHandle | None = None

    @property
    def user_count(self) -> int:
        return len(self._users)

    @property
    def group_count(self) -> int:
        return len(self._members)

    def get(self, user_id: int) -> UserEntry | None:
        return self._users.get(user_id)

    def members(self, group_key: str) -> tuple[int, ...]:
        return tuple(self._members.get(group_key, ()))

    def has_access(self, user_id: int, now: datetime) -> bool:
        entry = self._users.get(user_id)
        return entry is not None and has_access(entry.role, entry.access_until, now)

    def groups(self) -> dict[str, ScreenerSettings]:
        return {key: self._settings[key] for key in self._members}

    def replace_all(self, items: Iterable[tuple[UserEntry, ScreenerSettings]]) -> None:
        self._users.clear()
        self._members.clear()
        self._settings.clear()
        for entry, settings in items:
            self._add(entry, settings)
        self.compile_now()

    def upsert(self, entry: UserEntry, settings: ScreenerSettings) -> None:
        current = self._users.get(entry.user_id)
        if current == entry:
            return
        regroup = current is None or current.group_key != entry.group_key
        if current is not None:
            self._discard(current)
        self._add(entry, settings)
        if regroup:
            self._schedule()
        self._update_metrics()

    def remove(self, user_id: int) -> bool:
        current = self._users.get(user_id)
        if current is None:
            return False
        self._discard(current)
        self._schedule()
        self._update_metrics()
        return True

    def compile_now(self) -> None:
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None
        if self.on_groups_changed is not None:
            self.on_groups_changed(self.groups())
        self._update_metrics()

    def _add(self, entry: UserEntry, settings: ScreenerSettings) -> None:
        self._users[entry.user_id] = entry
        self._members.setdefault(entry.group_key, set()).add(entry.user_id)
        self._settings.setdefault(entry.group_key, settings)

    def _discard(self, entry: UserEntry) -> None:
        self._users.pop(entry.user_id, None)
        members = self._members.get(entry.group_key)
        if members is not None:
            members.discard(entry.user_id)
            if not members:
                del self._members[entry.group_key]
                self._settings.pop(entry.group_key, None)

    def _schedule(self) -> None:
        if self._handle is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.compile_now()
            return
        self._handle = loop.call_later(self._debounce, self.compile_now)

    def _update_metrics(self) -> None:
        metrics.commercial_users_eligible = len(self._users)
