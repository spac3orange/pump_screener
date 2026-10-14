"""Monitoring profiles against a real PostgreSQL (opt-in, destructive).

Set TEST_DATABASE_URL to a throwaway database whose name contains "test", e.g.
postgresql+asyncpg://screener:screener@localhost:55432/screener_test
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text

from screener.application.profiles import ProfileService
from screener.config import ScreenerSettings
from screener.domain.enums import SignalType
from screener.domain.profiles import ProfileNameTaken, ProfileProtected
from screener.infrastructure.db.models import SignalRow, SystemEventRow
from screener.infrastructure.db.profile_repo import ProfileRepository
from screener.infrastructure.db.repositories import Repository, load_effective_settings
from screener.infrastructure.db.session import create_engine, create_session_factory
from tests.helpers import make_signal

URL = os.getenv("TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    "test" not in URL.rsplit("/", 1)[-1],
    reason="TEST_DATABASE_URL to a *test* database is required",
)

# What a user had configured before profiles existed (only the rule fields).
LEGACY_ROW = {
    "enable_long": True,
    "long_threshold_pct": 18.0,
    "long_interval_sec": 600,
    "enable_short": False,
    "short_threshold_pct": 48.0,
    "short_interval_sec": 1800,
    "enable_dump": True,
    "dump_threshold_pct": 15.0,
    "dump_interval_sec": 600,
    "exclude_symbols": ["XUSDT", "AUSDT"],
}


def _alembic(revision: str) -> None:
    from alembic.config import Config

    from alembic import command

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", URL)
    command.upgrade(cfg, revision)


@pytest.fixture
async def db(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", URL)
    engine = create_engine(URL)
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await asyncio.to_thread(_alembic, "003")
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO screener_settings (id, config) VALUES (1, CAST(:c AS jsonb))"),
            {"c": json.dumps(LEGACY_ROW)},
        )
    await asyncio.to_thread(_alembic, "head")
    sf = create_session_factory(engine)
    yield sf
    await engine.dispose()


def _service(sf) -> tuple[ProfileService, Repository]:
    repo = Repository(sf)
    return ProfileService(ProfileRepository(sf), event_log=repo.log_event), repo


@pytest.mark.asyncio
async def test_migration_seeds_default_with_identical_settings(db) -> None:
    defaults = ScreenerSettings(min_turnover_24h_usdt=250_000, cooldown_sec=120)
    repo = Repository(db)
    before = await load_effective_settings(repo, defaults)

    svc, _ = _service(db)
    active = await svc.bootstrap(lambda: load_effective_settings(repo, defaults))

    assert active.profile is not None and active.profile.is_system
    assert active.profile.name == "Default"
    assert active.settings == before
    assert active.settings.long_threshold_pct == 18.0
    assert active.settings.min_turnover_24h_usdt == 250_000
    assert active.profile.current.config_hash == before.config_hash()

    # Restart: same active profile, no second Default, .env changes do not leak in.
    svc2, _ = _service(db)
    again = await svc2.bootstrap(
        lambda: load_effective_settings(repo, ScreenerSettings(long_threshold_pct=99))
    )
    assert again.profile is not None and again.profile.id == active.profile.id
    assert again.settings == before
    assert len(await svc2.list_profiles()) == 1


@pytest.mark.asyncio
async def test_profile_lifecycle_and_signal_provenance(db) -> None:
    repo = Repository(db)
    svc, _ = _service(db)
    default = await svc.bootstrap(lambda: load_effective_settings(repo, ScreenerSettings()))
    assert default.profile is not None

    aggressive = await svc.clone(default.profile.id, "Aggressive", chat_id=1)
    with pytest.raises(ProfileNameTaken):
        await svc.create_from_current("aggressive")
    await svc.activate(aggressive.id, chat_id=1)
    edited = await svc.update_active_settings(
        svc.active.settings.model_copy(update={"long_threshold_pct": 12.0}), chat_id=1
    )
    assert edited.profile is not None and edited.profile.current.version_no == 2

    signal_ctx = edited.context
    signal = replace(
        make_signal(SignalType.PUMP, 600, 13.0),
        rule_version=signal_ctx.rule_version,
        monitoring_profile_id=signal_ctx.profile_id,
        monitoring_profile_version_id=signal_ctx.profile_version_id,
        monitoring_profile_name=signal_ctx.profile_name,
        monitoring_profile_version_no=signal_ctx.profile_version_no,
        detected_at=datetime.now(UTC),
    )
    await repo.save_signal(signal)

    # Editing again must not rewrite the version the stored signal points to.
    await svc.update_active_settings(
        svc.active.settings.model_copy(update={"long_threshold_pct": 30.0}), chat_id=1
    )
    store = ProfileRepository(db)
    async with db() as session:
        row = await session.scalar(select(SignalRow).where(SignalRow.id == signal.id))
        assert row is not None
        assert row.monitoring_profile_version_id == signal_ctx.profile_version_id
        assert row.payload["monitoring_profile"]["name"] == "Aggressive"
        events = (
            await session.scalars(
                select(SystemEventRow.event_type).where(SystemEventRow.component == "profiles")
            )
        ).all()
    version = await store.get_version(row.monitoring_profile_version_id)
    assert version is not None and version.config["long_threshold_pct"] == 12.0
    assert "monitoring_profile_changed" in events

    renamed = await svc.rename(aggressive.id, "Fast Pumps")
    assert svc.active.name == "Fast Pumps" and renamed.name == "Fast Pumps"
    with pytest.raises(ProfileProtected):
        await svc.delete(aggressive.id)
    with pytest.raises(ProfileProtected):
        await svc.delete(default.profile.id)
    await svc.activate(default.profile.id)
    await svc.delete(aggressive.id)
    names = [p.name for p in await svc.list_profiles()]
    assert names == ["Default"]
    # Deleted name can be reused; soft-deleted versions stay for provenance.
    await svc.create_from_current("Fast Pumps")
    assert await store.get_version(row.monitoring_profile_version_id) is not None
