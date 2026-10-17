"""Migration 007 up/down keeps existing Bybit rows."""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

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
async def test_007_roundtrip_keeps_existing_rows() -> None:
    engine = create_engine(URL)
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await asyncio.to_thread(_alembic, "upgrade", "006")

    signal_id = uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO instruments "
                "(symbol, contract_type, status, settle_coin, "
                "base_coin, quote_coin, is_pre_listing) "
                "VALUES ('BTCUSDT', 'LinearPerpetual', 'Trading', "
                "'USDT', 'BTC', 'USDT', false)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO signals "
                "(id, symbol, signal_type, interval_sec, change_pct, price, "
                "prev_price, rule_version, payload, detected_at) "
                "VALUES (:id, 'BTCUSDT', 'PUMP', 720, 41.0, 1.4, 1.0, 'v', "
                "CAST('{}' AS jsonb), now())"
            ),
            {"id": signal_id},
        )
        await conn.execute(
            text("INSERT INTO users (telegram_user_id, chat_id) VALUES (1001, 1001)")
        )

    await asyncio.to_thread(_alembic, "upgrade", "007")
    async with engine.connect() as conn:
        inst = (
            await conn.execute(text("SELECT exchange, symbol FROM instruments"))
        ).one()
        assert inst == ("bybit", "BTCUSDT")
        sig = (
            await conn.execute(
                text("SELECT exchange FROM signals WHERE id = :id"), {"id": signal_id}
            )
        ).scalar_one()
        assert sig == "bybit"
        exchanges = (
            await conn.execute(
                text("SELECT enabled_exchanges FROM users WHERE telegram_user_id = 1001")
            )
        ).scalar_one()
        assert set(exchanges) == {"bybit", "binance"}
        exists = (
            await conn.execute(text("SELECT to_regclass('public.app_settings')"))
        ).scalar_one()
        assert exists == "app_settings"
        pk = (
            await conn.execute(
                text(
                    "SELECT a.attname FROM pg_index i "
                    "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
                    "WHERE i.indrelid = 'instruments'::regclass AND i.indisprimary"
                )
            )
        ).scalars().all()
        assert set(pk) == {"exchange", "symbol"}

    await asyncio.to_thread(_alembic, "downgrade", "006")
    async with engine.connect() as conn:
        symbols = (await conn.execute(text("SELECT symbol FROM instruments"))).scalars().all()
        assert symbols == ["BTCUSDT"]
        remaining = (await conn.execute(text("SELECT id FROM signals"))).scalars().all()
        assert remaining == [signal_id]
        users = (await conn.execute(text("SELECT telegram_user_id FROM users"))).scalars().all()
        assert users == [1001]
        cols = (
            await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'signals' AND column_name = 'exchange'"
                )
            )
        ).scalars().all()
        assert cols == []
        exists = (
            await conn.execute(text("SELECT to_regclass('public.app_settings')"))
        ).scalar_one()
        assert exists is None
    await engine.dispose()
