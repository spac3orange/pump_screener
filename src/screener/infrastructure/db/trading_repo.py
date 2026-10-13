from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from screener.domain.trading import ExecutionStatus
from screener.infrastructure.db.models import TradingExecutionRow, TradingRuntimeStateRow
from screener.infrastructure.db.session import session_scope


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
