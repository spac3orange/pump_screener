from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from screener.application.instrument_registry import InstrumentRegistry
from screener.application.market_processor import MarketDataProcessor
from screener.application.notification_service import NotificationService
from screener.application.price_history import PriceHistoryManager
from screener.application.signal_engine import SignalEngine
from screener.config import ScreenerSettings, SystemSettings
from screener.infrastructure.bybit.rest import BybitRestClient
from screener.infrastructure.bybit.ws import WebSocketManager
from screener.infrastructure.db.repositories import Repository


@dataclass
class AppState:
    system: SystemSettings
    repo: Repository
    rest: BybitRestClient
    history: PriceHistoryManager
    processor: MarketDataProcessor
    signal_engine: SignalEngine
    registry: InstrumentRegistry
    ws_manager: WebSocketManager
    notifications: NotificationService
    extras: dict[str, Any] = field(default_factory=dict)

    async def apply_settings(self, settings: ScreenerSettings, *, chat_id: int | None = None) -> None:
        self.signal_engine.update_settings(settings)
        self.registry.set_exclude_symbols(settings.exclude_symbols)
        self.processor._max_tick_age_ms = settings.max_tick_age_ms
        await self.repo.save_screener_settings(settings.model_dump(mode="json"), chat_id=chat_id)
