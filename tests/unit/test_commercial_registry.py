from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from screener.application.commercial.registry import ActiveUserRegistry, UserEntry
from screener.domain.commercial import UserRole
from tests.helpers import settings

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
A = settings(long_threshold_pct=5.0)
B = settings(long_threshold_pct=6.0)


def entry(uid: int, cfg=A, *, until=NOW + timedelta(hours=1), role=UserRole.USER) -> UserEntry:
    return UserEntry(uid, uid * 10, role, until, cfg.config_hash())


def test_replace_all_groups_identical_configs() -> None:
    compiled: list[dict] = []
    reg = ActiveUserRegistry(on_groups_changed=compiled.append)
    reg.replace_all([(entry(1), A), (entry(2), A), (entry(3, B), B)])
    assert reg.user_count == 3 and reg.group_count == 2
    assert set(reg.members(A.config_hash())) == {1, 2}
    assert set(compiled[-1]) == {A.config_hash(), B.config_hash()}


def test_remove_last_member_drops_group_and_compiles_without_loop() -> None:
    compiled: list[dict] = []
    reg = ActiveUserRegistry(on_groups_changed=compiled.append)
    reg.replace_all([(entry(1), A), (entry(3, B), B)])
    assert reg.remove(3)
    assert not reg.remove(3)
    assert set(compiled[-1]) == {A.config_hash()}
    assert reg.members(B.config_hash()) == ()


async def test_regroup_is_debounced_and_swapped_once() -> None:
    compiled: list[dict] = []
    reg = ActiveUserRegistry(on_groups_changed=compiled.append, debounce_sec=0.01)
    reg.replace_all([(entry(1), A)])
    n = len(compiled)
    reg.upsert(entry(2, B), B)
    reg.upsert(entry(1, B), B)
    # Membership is visible immediately; the engine index follows after the debounce.
    assert set(reg.members(B.config_hash())) == {1, 2}
    assert len(compiled) == n
    await asyncio.sleep(0.05)
    assert len(compiled) == n + 1
    assert set(compiled[-1]) == {B.config_hash()}


async def test_access_change_without_regroup_does_not_recompile() -> None:
    compiled: list[dict] = []
    reg = ActiveUserRegistry(on_groups_changed=compiled.append, debounce_sec=0.01)
    reg.replace_all([(entry(1), A)])
    n = len(compiled)
    reg.upsert(entry(1, until=NOW + timedelta(days=30)), A)
    await asyncio.sleep(0.03)
    assert len(compiled) == n
    assert reg.get(1).access_until == NOW + timedelta(days=30)


def test_has_access_uses_real_time() -> None:
    reg = ActiveUserRegistry()
    reg.replace_all([(entry(1, until=NOW), A), (entry(2, role=UserRole.ADMIN, until=None), A)])
    assert reg.has_access(1, NOW - timedelta(seconds=1))
    assert not reg.has_access(1, NOW)
    assert reg.has_access(2, NOW + timedelta(days=999))
    assert not reg.has_access(99, NOW)
