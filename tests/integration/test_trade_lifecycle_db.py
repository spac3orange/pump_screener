"""Migration 005 + TradingRepository lifecycle methods on real PostgreSQL (opt-in, destructive).

Set TEST_DATABASE_URL to a throwaway database whose name contains "test".
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text

from screener.domain.trade_lifecycle import PositionCloseReason, TradeClosedEvent
from screener.infrastructure.db.session import create_engine, create_session_factory
from screener.infrastructure.db.trading_repo import TradingRepository

URL = os.getenv("TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    "test" not in URL.rsplit("/", 1)[-1],
    reason="TEST_DATABASE_URL to a *test* database is required",
)

_INSERT = text(
    "INSERT INTO trading_executions (id, signal_id, symbol, signal_type, configured_action, "
    "side, mode, status, order_link_id, bybit_order_id, filled_qty, avg_fill_price, "
    "take_profit, stop_loss, created_at, updated_at) VALUES (:id, :sid, :symbol, 'PUMP', "
    "'SHORT', :side, 'demo', :status, :link, :oid, :qty, :avg, 0.3, 0.33, :created, :created)"
)


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
    await asyncio.to_thread(_alembic, "004")
    created = datetime.now(UTC) - timedelta(hours=1)
    ids = {"filled": uuid4(), "rejected": uuid4()}
    async with engine.begin() as conn:
        for key, status, qty in (("filled", "FILLED", 100.0), ("rejected", "REJECTED", None)):
            await conn.execute(
                _INSERT,
                {
                    "id": ids[key],
                    "sid": uuid4(),
                    "symbol": "EPICUSDT" if key == "filled" else "BTCUSDT",
                    "side": "Sell",
                    "status": status,
                    "link": f"link-{key}",
                    "oid": f"order-{key}",
                    "qty": qty,
                    "avg": 0.3102 if qty else None,
                    "created": created,
                },
            )
    await asyncio.to_thread(_alembic, "head")
    yield create_session_factory(engine), engine, ids, created
    await engine.dispose()


def _event(trade, **kw) -> TradeClosedEvent:
    base = dict(
        trade_id=trade.trade_id,
        signal_id=trade.signal_id,
        symbol=trade.symbol,
        side=trade.side,
        mode=trade.mode,
        close_reason=PositionCloseReason.STOP_LOSS,
        reason_detail="сработал Stop Loss",
        entry_price=trade.entry_price,
        exit_price=Decimal("0.3195"),
        qty=trade.qty,
        closed_qty=trade.qty,
        opened_at=trade.opened_at,
        closed_at=datetime.now(UTC),
        realized_pnl=Decimal("-0.93"),
        fees=Decimal("0.035123"),
        funding=Decimal("0"),
        net_pnl=Decimal("-0.965123"),
        bybit_closed_pnl=Decimal("-0.965"),
        balance_after=Decimal("9.79"),
        close_order_id="sl-1",
        close_execution_id="e-1",
    )
    base.update(kw)
    return TradeClosedEvent(**base)


async def test_migration_keeps_rows_and_backfills_opened_at(db) -> None:
    _sf, engine, ids, created = db
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT id, status, opened_at, close_reason, close_recovered, "
                    "close_notified_at FROM trading_executions"
                )
            )
        ).all()
    by_id = {r.id: r for r in rows}
    assert by_id[ids["filled"]].status == "FILLED"
    assert abs(by_id[ids["filled"]].opened_at - created) < timedelta(seconds=1)
    assert by_id[ids["rejected"]].opened_at is None
    assert all(r.close_reason is None and r.close_recovered is False for r in rows)


async def test_repository_lifecycle_is_atomic_and_exactly_once(db) -> None:
    sf, _engine, ids, _created = db
    repo = TradingRepository(sf)

    tracked = await repo.list_tracked_trades("demo")
    assert [t.trade_id for t in tracked] == [ids["filled"]]  # REJECTED is not a position
    assert await repo.list_tracked_trades("mainnet") == []
    trade = await repo.find_tracked_trade("EPICUSDT", "demo")
    assert trade is not None and trade.qty == Decimal("100") and trade.side == "Sell"
    assert trade.entry_order_id == "order-filled" and trade.order_link_id == "link-filled"

    await repo.mark_open(
        trade.trade_id, remaining_qty=Decimal("100"), tp_order_id="tp", sl_order_id="sl"
    )
    await repo.update_remaining(trade.trade_id, Decimal("40"))
    reopened = await repo.find_tracked_trade("EPICUSDT", "demo")
    assert reopened is not None and reopened.status == "OPEN" and reopened.tp_order_id == "tp"
    await repo.mark_closing(trade.trade_id)

    event = _event(trade)
    assert await repo.finalize_close(event) is True
    late = _event(trade, close_reason=PositionCloseReason.UNKNOWN)
    assert await repo.finalize_close(late) is False
    await repo.mark_open(
        trade.trade_id, remaining_qty=Decimal("1"), tp_order_id=None, sl_order_id=None
    )
    assert await repo.find_tracked_trade("EPICUSDT", "demo") is None

    since = datetime.now(UTC) - timedelta(days=1)
    pending = await repo.list_unnotified_closed("demo", since)
    assert [e.trade_id for e in pending] == [trade.trade_id]
    stored = pending[0]
    assert stored.close_reason is PositionCloseReason.STOP_LOSS
    assert stored.fees == Decimal("0.035123") and stored.net_pnl == Decimal("-0.965123")
    assert stored.close_order_id == "sl-1" and stored.balance_after == Decimal("9.79")

    claims = await asyncio.gather(
        *(repo.claim_close_notification(trade.trade_id) for _ in range(5))
    )
    assert claims.count(True) == 1
    assert await repo.list_unnotified_closed("demo", since) == []
    await repo.release_close_notification(trade.trade_id)
    assert await repo.claim_close_notification(trade.trade_id) is True

    last = await repo.last_closed("demo")
    assert last is not None and last.trade_id == trade.trade_id
