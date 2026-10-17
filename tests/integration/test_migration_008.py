"""Migration 008 appends bitget and downgrade removes only that element."""

from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy import text

from screener.infrastructure.db.session import create_engine

URL = os.getenv("TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    "test" not in URL.rsplit("/", 1)[-1],
    reason="TEST_DATABASE_URL to a *test* database is required",
)


def _alembic(action: str, revision: str) -> None:
    from alembic.config import Config

    from alembic import command

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", URL)
    getattr(command, action)(cfg, revision)


@pytest.mark.asyncio
async def test_008_appends_bitget_and_downgrade_removes_it() -> None:
    engine = create_engine(URL)
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await asyncio.to_thread(_alembic, "upgrade", "007")

    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO users (telegram_user_id, chat_id) VALUES (1001, 1001)")
        )

    await asyncio.to_thread(_alembic, "upgrade", "008")
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO users (telegram_user_id, chat_id) VALUES (1002, 1002)")
        )
    async with engine.connect() as conn:
        old = (
            await conn.execute(
                text("SELECT enabled_exchanges FROM users WHERE telegram_user_id = 1001")
            )
        ).scalar_one()
        new = (
            await conn.execute(
                text("SELECT enabled_exchanges FROM users WHERE telegram_user_id = 1002")
            )
        ).scalar_one()
        default = (
            await conn.execute(
                text(
                    "SELECT column_default FROM information_schema.columns "
                    "WHERE table_name = 'users' AND column_name = 'enabled_exchanges'"
                )
            )
        ).scalar_one()
        assert set(old) == {"bybit", "binance", "bitget"}
        assert set(new) == {"bybit", "binance", "bitget"}
        assert "bitget" in default

    await asyncio.to_thread(_alembic, "downgrade", "007")
    async with engine.connect() as conn:
        old = (
            await conn.execute(
                text("SELECT enabled_exchanges FROM users WHERE telegram_user_id = 1001")
            )
        ).scalar_one()
        default = (
            await conn.execute(
                text(
                    "SELECT column_default FROM information_schema.columns "
                    "WHERE table_name = 'users' AND column_name = 'enabled_exchanges'"
                )
            )
        ).scalar_one()
        assert set(old) == {"bybit", "binance"}
        assert "bitget" not in default
    await engine.dispose()
