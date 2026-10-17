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
from screener.domain.models import Instrument, MarketSignal
from screener.infrastructure.db.models import (
    AppSettingsRow,
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
                    exchange=getattr(inst, "exchange", "bybit") or "bybit",
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
                    index_elements=[InstrumentRow.exchange, InstrumentRow.symbol],
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

    async def save_signal(self, signal: MarketSignal) -> None:
        async with session_scope(self._sf) as session:
            row = SignalRow(
                id=signal.id,
                exchange=signal.exchange,
                symbol=signal.symbol,
                signal_type=signal.signal_type.value,
                interval_sec=signal.interval_sec,
                change_pct=signal.change_pct,
                price=signal.price,
                prev_price=signal.prev_price,
                turnover_24h=signal.turnover_24h,
                volume_ratio=None,
                rule_version=signal.rule_version,
                payload=signal.to_payload(),
                detected_at=signal.detected_at,
                monitoring_profile_id=signal.monitoring_profile_id,
                monitoring_profile_version_id=signal.monitoring_profile_version_id,
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

    async def count_symbol_signals_24h(
        self,
        symbol: str,
        *,
        now: datetime,
        exclude_id: UUID | None = None,
        exchange: str = "bybit",
    ) -> int:
        since = now - timedelta(hours=24)
        conditions = [
            SignalRow.symbol == symbol,
            SignalRow.exchange == exchange,
            SignalRow.detected_at >= since,
        ]
        if exclude_id is not None:
            conditions.append(SignalRow.id != exclude_id)
        async with session_scope(self._sf) as session:
            result = await session.execute(
                select(func.count()).select_from(SignalRow).where(*conditions)
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
            result = await session.execute(
                select(SignalRow.signal_type, func.count()).group_by(SignalRow.signal_type)
            )
            counts = {signal_type: int(n) for signal_type, n in result.all()}
            return {"total": sum(counts.values()), "by_type": counts}

    async def get_app_setting(self, key: str) -> dict[str, Any] | None:
        async with session_scope(self._sf) as session:
            row = await session.get(AppSettingsRow, key)
            return None if row is None else dict(row.value)

    async def set_app_setting(self, key: str, value: dict[str, Any]) -> None:
        async with session_scope(self._sf) as session:
            now = datetime.now(UTC)
            stmt = insert(AppSettingsRow).values(key=key, value=value, updated_at=now)
            stmt = stmt.on_conflict_do_update(
                index_elements=[AppSettingsRow.key],
                set_={"value": value, "updated_at": now},
            )
            await session.execute(stmt)


def _has_rule_profile(stored: dict[str, Any]) -> bool:
    return "long_interval_sec" in stored and "short_interval_sec" in stored


async def load_effective_settings(
    repo: Repository, defaults: ScreenerSettings
) -> ScreenerSettings:
    """Pre-profile effective config (env defaults + legacy row); seeds ``Default`` once."""
    stored = await repo.load_screener_settings()
    if not stored or not _has_rule_profile(stored):
        data = defaults.model_dump(mode="json")
        if stored and "exclude_symbols" in stored:
            data["exclude_symbols"] = stored["exclude_symbols"]
        return ScreenerSettings.model_validate(data)
    merged = defaults.model_dump(mode="json")
    merged.update(stored)
    return ScreenerSettings.model_validate(merged)
