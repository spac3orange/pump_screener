from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from screener.application.correlator import SignalCorrelator
from screener.application.instrument_registry import InstrumentRegistry
from screener.application.market_processor import MarketDataProcessor
from screener.application.notification_service import NotificationService
from screener.application.persistence_service import PersistenceService
from screener.application.price_history import PriceHistoryManager
from screener.application.signal_engine import SignalEngine
from screener.application.trading.service import TradingService
from screener.config import ScreenerSettings, SystemSettings, TradingSettings
from screener.domain.models import MarketSignal
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
    correlator: SignalCorrelator
    registry: InstrumentRegistry
    ws_manager: WebSocketManager
    persistence: PersistenceService
    notifications: NotificationService
    trading: TradingService | None = None
    trading_settings: TradingSettings | None = None
    extras: dict[str, Any] = field(default_factory=dict)
    pending_edits: dict[int, tuple[str, str]] = field(default_factory=dict)

    def dispatch_signal(self, signal: MarketSignal) -> None:
        """Hand a MarketSignal to persistence, Telegram, and optional trading."""
        self.persistence.enqueue_signal(signal)
        self.notifications.enqueue(signal)
        if self.trading is not None:
            self.trading.enqueue(signal)

    def begin_setting_edit(self, user_id: int, kind: str, field_name: str) -> None:
        self.pending_edits[user_id] = (kind, field_name)

    def pending_edit(self, user_id: int) -> tuple[str, str] | None:
        return self.pending_edits.get(user_id)

    def clear_pending_edit(self, user_id: int) -> None:
        self.pending_edits.pop(user_id, None)

    async def apply_settings(
        self, settings: ScreenerSettings, *, chat_id: int | None = None
    ) -> None:
        self.signal_engine.update_settings(settings)
        self.correlator.window_ms = settings.correlation_window_ms
        self.registry.set_exclude_symbols(settings.exclude_symbols)
        await self.repo.save_screener_settings(settings.model_dump(mode="json"), chat_id=chat_id)
