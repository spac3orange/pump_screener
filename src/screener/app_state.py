from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from screener.application.correlator import SignalCorrelator
from screener.application.instrument_registry import InstrumentRegistry
from screener.application.market_processor import MarketDataProcessor
from screener.application.notification_service import NotificationService
from screener.application.persistence_service import PersistenceService
from screener.application.price_history import PriceHistoryManager
from screener.application.profiles import ActiveProfile, ProfileService
from screener.application.signal_engine import SignalEngine
from screener.application.trading.service import TradingService
from screener.config import ScreenerSettings, SystemSettings, TradingSettings
from screener.domain.models import MarketSignal
from screener.infrastructure.bybit.rest import BybitRestClient
from screener.infrastructure.bybit.ws import WebSocketManager
from screener.infrastructure.db.repositories import Repository
from screener.logging import log

logger = logging.getLogger(__name__)


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
    profiles: ProfileService | None = None
    extras: dict[str, Any] = field(default_factory=dict)
    # user_id -> (kind, field); one pending text prompt per admin, so bounded by ADMIN_ID.
    pending_edits: dict[int, tuple[str, str]] = field(default_factory=dict)

    def dispatch_signal(self, signal: MarketSignal) -> None:
        """Hand a MarketSignal to persistence, Telegram, and optional trading."""
        log(
            logger,
            logging.DEBUG,
            f"Dispatch signal {signal.symbol} {signal.signal_type.value} id={signal.id}",
            component="app_state",
            event="dispatch_signal",
            symbol=signal.symbol,
            signal_id=str(signal.id),
            signal_type=signal.signal_type.value,
        )
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

    def apply_profile(self, active: ActiveProfile, reset_dedup: bool) -> None:
        """Synchronous swap of the monitoring config (no await => no interleaved event)."""
        settings = active.settings
        self.signal_engine.update_settings(
            settings, context=active.context, reset_dedup=reset_dedup
        )
        self.correlator.window_ms = settings.correlation_window_ms
        self.registry.set_exclude_symbols(settings.exclude_symbols)

    @property
    def active_profile_name(self) -> str:
        return self.profiles.active.name if self.profiles is not None else "—"

    async def apply_settings(
        self, settings: ScreenerSettings, *, chat_id: int | None = None
    ) -> None:
        """``/settings`` edit of the active monitoring profile."""
        if self.profiles is not None:
            await self.profiles.update_active_settings(settings, chat_id=chat_id)
            return
        self.apply_profile(ActiveProfile(settings=settings, profile=None), False)
