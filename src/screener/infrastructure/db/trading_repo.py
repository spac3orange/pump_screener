from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from screener.domain.trade_lifecycle import OpenTrade, PositionCloseReason, TradeClosedEvent
from screener.domain.trading import TRACKED_STATUSES, ExecutionStatus
from screener.infrastructure.db.models import TradingExecutionRow, TradingRuntimeStateRow
from screener.infrastructure.db.session import session_scope

_CLOSED = ExecutionStatus.CLOSED.value
_TRACKED = sorted(s.value for s in TRACKED_STATUSES)


def _dec(value: float | Decimal | None) -> Decimal | None:
    if value is None:
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _opened_at(row: TradingExecutionRow) -> datetime:
    return row.opened_at or row.created_at


def _to_open_trade(row: TradingExecutionRow) -> OpenTrade:
    return OpenTrade(
        trade_id=row.id,
        signal_id=row.signal_id,
        symbol=row.symbol,
        side=row.side or "",
        qty=_dec(row.filled_qty) or Decimal("0"),
        entry_price=_dec(row.avg_fill_price),
        entry_order_id=row.bybit_order_id,
        order_link_id=row.order_link_id,
        opened_at=_opened_at(row),
        status=row.status,
        mode=row.mode,
        take_profit=_dec(row.take_profit),
        stop_loss=_dec(row.stop_loss),
        tp_order_id=row.tp_order_id,
        sl_order_id=row.sl_order_id,
    )


def _to_closed_event(row: TradingExecutionRow) -> TradeClosedEvent:
    try:
        reason = PositionCloseReason(row.close_reason or "UNKNOWN")
    except ValueError:
        reason = PositionCloseReason.UNKNOWN
    return TradeClosedEvent(
        trade_id=row.id,
        signal_id=row.signal_id,
        symbol=row.symbol,
        side=row.side or "",
        mode=row.mode,
        close_reason=reason,
        reason_detail=row.close_reason_detail or "",
        entry_price=_dec(row.avg_fill_price),
        exit_price=row.exit_price,
        qty=_dec(row.filled_qty) or Decimal("0"),
        closed_qty=row.closed_qty or Decimal("0"),
        opened_at=_opened_at(row),
        closed_at=row.closed_at or row.updated_at,
        realized_pnl=row.realized_pnl,
        fees=row.trading_fee,
        funding=row.funding_fee,
        net_pnl=row.net_pnl,
        bybit_closed_pnl=row.bybit_closed_pnl,
        balance_after=row.balance_after,
        close_order_id=row.close_order_id,
        close_execution_id=row.close_execution_id,
        recovered=bool(row.close_recovered),
    )


class TradingRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def try_reserve(
        self,
        *,
        signal_id: UUID,
        symbol: str,
        signal_type: str,
        configured_action: str,
        side: str | None,
        mode: str,
        order_link_id: str,
    ) -> bool:
        """Insert RESERVED row. Returns False if signal_id already reserved."""
        async with session_scope(self._sf) as session:
            stmt = (
                insert(TradingExecutionRow)
                .values(
                    id=uuid4(),
                    signal_id=signal_id,
                    symbol=symbol,
                    signal_type=signal_type,
                    configured_action=configured_action,
                    side=side,
                    mode=mode,
                    status=ExecutionStatus.RESERVED.value,
                    order_link_id=order_link_id,
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
                .on_conflict_do_nothing(index_elements=[TradingExecutionRow.signal_id])
            )
            result = await session.execute(stmt)
            return bool(result.rowcount)

    async def update_execution(self, signal_id: UUID, **fields: Any) -> None:
        async with session_scope(self._sf) as session:
            result = await session.execute(
                select(TradingExecutionRow).where(TradingExecutionRow.signal_id == signal_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                return
            for key, value in fields.items():
                if hasattr(row, key):
                    setattr(row, key, value)
            row.updated_at = datetime.now(UTC)

    async def get_by_signal(self, signal_id: UUID) -> TradingExecutionRow | None:
        async with session_scope(self._sf) as session:
            result = await session.execute(
                select(TradingExecutionRow).where(TradingExecutionRow.signal_id == signal_id)
            )
            return result.scalar_one_or_none()

    async def get_runtime_enabled(self) -> bool:
        async with session_scope(self._sf) as session:
            row = await session.get(TradingRuntimeStateRow, 1)
            return bool(row.enabled) if row else False

    async def set_runtime_enabled(self, enabled: bool, *, chat_id: int | None = None) -> None:
        async with session_scope(self._sf) as session:
            row = await session.get(TradingRuntimeStateRow, 1)
            if row is None:
                session.add(
                    TradingRuntimeStateRow(
                        id=1,
                        enabled=enabled,
                        updated_by_chat_id=chat_id,
                        updated_at=datetime.now(UTC),
                    )
                )
                return
            row.enabled = enabled
            row.updated_by_chat_id = chat_id
            row.updated_at = datetime.now(UTC)

    # --- position lifecycle (LifecycleStore) ------------------------------------------

    def _tracked_query(self, mode: str):  # noqa: ANN202
        t = TradingExecutionRow
        return select(t).where(
            t.mode == mode,
            t.status.in_(_TRACKED),
            t.filled_qty > 0,
            t.side.is_not(None),
        )

    async def list_tracked_trades(self, mode: str) -> list[OpenTrade]:
        async with session_scope(self._sf) as session:
            result = await session.execute(
                self._tracked_query(mode).order_by(TradingExecutionRow.created_at)
            )
            return [_to_open_trade(row) for row in result.scalars()]

    async def find_tracked_trade(self, symbol: str, mode: str) -> OpenTrade | None:
        async with session_scope(self._sf) as session:
            result = await session.execute(
                self._tracked_query(mode)
                .where(TradingExecutionRow.symbol == symbol)
                .order_by(TradingExecutionRow.created_at.desc())
                .limit(1)
            )
            row = result.scalar_one_or_none()
            return _to_open_trade(row) if row is not None else None

    async def _update_unclosed(self, trade_id: UUID, **values: Any) -> bool:
        t = TradingExecutionRow
        async with session_scope(self._sf) as session:
            result = await session.execute(
                update(t)
                .where(t.id == trade_id, t.status != _CLOSED)
                .values(updated_at=datetime.now(UTC), **values)
            )
            return bool(result.rowcount)

    async def mark_open(
        self,
        trade_id: UUID,
        *,
        remaining_qty: Decimal,
        tp_order_id: str | None,
        sl_order_id: str | None,
    ) -> None:
        t = TradingExecutionRow
        await self._update_unclosed(
            trade_id,
            status=ExecutionStatus.OPEN.value,
            remaining_qty=remaining_qty,
            tp_order_id=tp_order_id,
            sl_order_id=sl_order_id,
            opened_at=func.coalesce(t.opened_at, t.created_at),
        )

    async def update_remaining(self, trade_id: UUID, remaining_qty: Decimal) -> None:
        await self._update_unclosed(trade_id, remaining_qty=remaining_qty)

    async def mark_closing(self, trade_id: UUID) -> None:
        await self._update_unclosed(trade_id, status=ExecutionStatus.CLOSING.value)

    async def finalize_close(self, event: TradeClosedEvent) -> bool:
        """Atomic OPEN/CLOSING → CLOSED; False if another path already closed it."""
        t = TradingExecutionRow
        return await self._update_unclosed(
            event.trade_id,
            status=_CLOSED,
            closed_at=event.closed_at,
            close_reason=event.close_reason.value,
            close_reason_detail=event.reason_detail[:200],
            exit_price=event.exit_price,
            closed_qty=event.closed_qty,
            remaining_qty=Decimal("0"),
            realized_pnl=event.realized_pnl,
            trading_fee=event.fees,
            funding_fee=event.funding,
            net_pnl=event.net_pnl,
            bybit_closed_pnl=event.bybit_closed_pnl,
            balance_after=event.balance_after,
            close_order_id=event.close_order_id,
            close_execution_id=event.close_execution_id,
            close_recovered=event.recovered,
            opened_at=func.coalesce(t.opened_at, t.created_at),
        )

    async def claim_close_notification(self, trade_id: UUID) -> bool:
        """Persistent exactly-once guard: only one caller ever wins the claim."""
        t = TradingExecutionRow
        async with session_scope(self._sf) as session:
            result = await session.execute(
                update(t)
                .where(t.id == trade_id, t.status == _CLOSED, t.close_notified_at.is_(None))
                .values(close_notified_at=datetime.now(UTC))
            )
            return bool(result.rowcount)

    async def release_close_notification(self, trade_id: UUID) -> None:
        t = TradingExecutionRow
        async with session_scope(self._sf) as session:
            await session.execute(
                update(t).where(t.id == trade_id).values(close_notified_at=None)
            )

    async def list_unnotified_closed(self, mode: str, since: datetime) -> list[TradeClosedEvent]:
        t = TradingExecutionRow
        async with session_scope(self._sf) as session:
            result = await session.execute(
                select(t)
                .where(
                    t.mode == mode,
                    t.status == _CLOSED,
                    t.close_notified_at.is_(None),
                    t.closed_at >= since,
                )
                .order_by(t.closed_at)
                .limit(50)
            )
            return [_to_closed_event(row) for row in result.scalars()]

    async def last_closed(self, mode: str) -> TradeClosedEvent | None:
        t = TradingExecutionRow
        async with session_scope(self._sf) as session:
            result = await session.execute(
                select(t)
                .where(t.mode == mode, t.status == _CLOSED, t.closed_at.is_not(None))
                .order_by(t.closed_at.desc())
                .limit(1)
            )
            row = result.scalar_one_or_none()
            return _to_closed_event(row) if row is not None else None
