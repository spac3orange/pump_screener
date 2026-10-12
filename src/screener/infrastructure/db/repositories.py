from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from screener.config import ScreenerSettings
from screener.domain.enums import DeliveryStatus
from screener.domain.models import Instrument, SignalEvent
from screener.infrastructure.db.models import (
    InstrumentRow,
    NotificationRow,
    ScreenerSettingsRow,
    SignalRow,
    SystemEventRow,
)
from screener.infrastructure.db.session import session_scope


class Repository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def upsert_instruments(self, instruments: Sequence[Instrument]) -> None:
        if not instruments:
            return
        async with session_scope(self._sf) as session:
            now = datetime.now(UTC)
            for inst in instruments:
                stmt = insert(InstrumentRow).values(
                    symbol=inst.symbol,
                    contract_type=inst.contract_type,
                    status=inst.status,
                    settle_coin=inst.settle_coin,
                    base_coin=inst.base_coin,
                    quote_coin=inst.quote_coin,
                    symbol_type=inst.symbol_type,
                    is_pre_listing=inst.is_pre_listing,
                    updated_at=now,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=[InstrumentRow.symbol],
                    set_={
                        "contract_type": inst.contract_type,
                        "status": inst.status,
                        "settle_coin": inst.settle_coin,
                        "base_coin": inst.base_coin,
                        "quote_coin": inst.quote_coin,
                        "symbol_type": inst.symbol_type,
                        "is_pre_listing": inst.is_pre_listing,
                        "updated_at": now,
                    },
                )
                await session.execute(stmt)

    async def save_signal(self, signal: SignalEvent) -> None:
        async with session_scope(self._sf) as session:
            row = SignalRow(
                id=signal.id,
                symbol=signal.symbol,
                signal_type=signal.signal_type.value,
                interval_sec=signal.interval_sec,
                change_pct=signal.change_pct,
                price=signal.price,
                prev_price=signal.prev_price,
                turnover_24h=signal.turnover_24h,
                volume_ratio=signal.volume_ratio,
                rule_version=signal.rule_version,
                payload=signal.to_payload(),
                detected_at=signal.detected_at,
            )
            session.add(row)

    async def save_notification(
        self,
        signal_id: UUID,
        chat_id: int,
        status: DeliveryStatus,
        error: str | None = None,
    ) -> None:
        async with session_scope(self._sf) as session:
            session.add(
                NotificationRow(
                    signal_id=signal_id,
                    chat_id=chat_id,
                    status=status.value,
                    error=error,
                )
            )

    async def log_event(
        self,
        *,
        level: str,
        component: str,
        event_type: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        async with session_scope(self._sf) as session:
            session.add(
                SystemEventRow(
                    level=level,
                    component=component,
                    event_type=event_type,
                    message=message,
                    details=details,
                )
            )

    async def load_screener_settings(self) -> dict[str, Any] | None:
        async with session_scope(self._sf) as session:
            row = await session.get(ScreenerSettingsRow, 1)
            return None if row is None else dict(row.config)

    async def save_screener_settings(
        self, config: dict[str, Any], *, chat_id: int | None = None
    ) -> None:
        async with session_scope(self._sf) as session:
            stmt = insert(ScreenerSettingsRow).values(
                id=1,
                config=config,
                updated_by_chat_id=chat_id,
                updated_at=datetime.now(UTC),
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[ScreenerSettingsRow.id],
                set_={
                    "config": config,
                    "updated_by_chat_id": chat_id,
                    "updated_at": datetime.now(UTC),
                },
            )
            await session.execute(stmt)

    async def count_symbol_signals_24h(self, symbol: str, *, now: datetime) -> int:
        since = now - timedelta(hours=24)
        async with session_scope(self._sf) as session:
            result = await session.execute(
                select(func.count())
                .select_from(SignalRow)
                .where(SignalRow.symbol == symbol, SignalRow.detected_at >= since)
            )
            return int(result.scalar_one())

    async def recent_signals(self, limit: int = 20) -> list[SignalRow]:
        async with session_scope(self._sf) as session:
            result = await session.execute(
                select(SignalRow).order_by(SignalRow.detected_at.desc()).limit(limit)
            )
            return list(result.scalars().all())

    async def signal_stats(self) -> dict[str, Any]:
        async with session_scope(self._sf) as session:
            result = await session.execute(select(SignalRow.signal_type, SignalRow.id))
            rows = result.all()
            counts: dict[str, int] = {}
            for signal_type, _ in rows:
                counts[signal_type] = counts.get(signal_type, 0) + 1
            return {"total": len(rows), "by_type": counts}


def _has_rule_profile(stored: dict[str, Any]) -> bool:
    return "long_interval_sec" in stored and "short_interval_sec" in stored


async def load_effective_settings(
    repo: Repository, defaults: ScreenerSettings
) -> ScreenerSettings:
    stored = await repo.load_screener_settings()
    if not stored or not _has_rule_profile(stored):
        data = defaults.model_dump(mode="json")
        if stored and "exclude_symbols" in stored:
            data["exclude_symbols"] = stored["exclude_symbols"]
        settings = ScreenerSettings.model_validate(data)
        await repo.save_screener_settings(settings.model_dump(mode="json"))
        return settings
    merged = defaults.model_dump(mode="json")
    merged.update(stored)
    return ScreenerSettings.model_validate(merged)
