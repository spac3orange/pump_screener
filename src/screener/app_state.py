from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from screener.application.correlator import SignalCorrelator
from screener.application.instrument_registry import InstrumentRegistry
from screener.application.market_processor import MarketDataProcessor
from screener.application.notification_service import NotificationService
from screener.application.persistence_service import PersistenceService
from screener.application.price_history import PriceHistoryManager
from screener.application.profiles import ActiveProfile, ProfileService
from screener.application.signal_engine import SignalEngine
from screener.application.trading.lifecycle import PositionLifecycleService
from screener.application.trading.service import TradingService
from screener.config import ScreenerSettings, SystemSettings, TradingSettings
from screener.domain.models import MarketSignal
from screener.infrastructure.bybit.rest import BybitRestClient
from screener.infrastructure.bybit.ws import WebSocketManager
from screener.infrastructure.db.repositories import Repository
from screener.logging import log

if TYPE_CHECKING:
    from screener.application.commercial.multi_engine import MultiConfigEngine
    from screener.commercial_runtime import CommercialRuntime

logger = logging.getLogger(__name__)

PENDING_EDIT_TTL_SEC = 600.0
PENDING_EDITS_MAX = 10_000


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
    lifecycle: PositionLifecycleService | None = None
    extras: dict[str, Any] = field(default_factory=dict)
    commercial: CommercialRuntime | None = None
    # telegram user_id -> (kind, field, monotonic deadline); bounded and expiring because
    # subscribers can open prompts too.
    pending_edits: dict[int, tuple[str, str, float]] = field(default_factory=dict)

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
        now = time.monotonic()
        if user_id not in self.pending_edits and len(self.pending_edits) >= PENDING_EDITS_MAX:
            self._prune_pending(now)
            while len(self.pending_edits) >= PENDING_EDITS_MAX:
                self.pending_edits.pop(next(iter(self.pending_edits)))
        self.pending_edits[user_id] = (kind, field_name, now + PENDING_EDIT_TTL_SEC)

    def pending_edit(self, user_id: int) -> tuple[str, str] | None:
        item = self.pending_edits.get(user_id)
        if item is None:
            return None
        if time.monotonic() >= item[2]:
            self.pending_edits.pop(user_id, None)
            return None
        return item[0], item[1]

    def _prune_pending(self, now: float) -> None:
        for uid in [u for u, item in self.pending_edits.items() if now >= item[2]]:
            del self.pending_edits[uid]

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
    def commercial_engine(self) -> MultiConfigEngine | None:
        return self.commercial.engine if self.commercial is not None else None

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
