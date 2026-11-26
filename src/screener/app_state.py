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
from screener.application.profile_monitor import ProfileMonitor
from screener.application.profiles import ActiveProfile, ProfileService
from screener.application.signal_engine import SignalEngine
from screener.application.trading.lifecycle import PositionLifecycleService
from screener.application.trading.service import TradingService
from screener.application.metrics import metrics
from screener.application.price_history import history_capacity_for
from screener.application.symbol_daily_limiter import SymbolDailyLimiter
from screener.config import ScreenerSettings, SystemSettings, TradingSettings
from screener.domain.exchanges import BYBIT
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
    admin_exchanges: set[str] = field(default_factory=lambda: {BYBIT})
    feeds: dict[str, Any] = field(default_factory=dict)
    symbol_daily_limiter: SymbolDailyLimiter = field(default_factory=SymbolDailyLimiter)
    profile_monitor: ProfileMonitor | None = None
    profile_monitors: list[ProfileMonitor] = field(default_factory=list)
    # telegram user_id -> (kind, field, monotonic deadline); bounded and expiring because
    # subscribers can open prompts too.
    pending_edits: dict[int, tuple[str, str, float]] = field(default_factory=dict)

    def dispatch_signal(self, signal: MarketSignal) -> None:
        """Bybit MarketSignal: persistence, Telegram (if the exchange is on), trading."""
        self._dispatch(signal, to_trading=True)

    def dispatch_monitor_signal(self, signal: MarketSignal) -> None:
        """Persistence and Telegram only; never trading (multi-profile admin path)."""
        self._dispatch(signal, to_trading=False)

    def _profile_settings_for_signal(self, signal: MarketSignal) -> ScreenerSettings | None:
        pid = signal.monitoring_profile_id
        if pid is None:
            return None
        key = str(pid)
        for monitor in self.profile_monitors:
            settings = monitor.settings_for(key)
            if settings is not None:
                return settings
        return None

    def _dispatch(self, signal: MarketSignal, *, to_trading: bool) -> None:
        limits: dict[str, int] = {}
        profile_settings = self._profile_settings_for_signal(signal)
        if profile_settings is not None:
            limits = profile_settings.symbol_daily_limits
        elif self.signal_engine is not None:
            limits = self.signal_engine.settings.symbol_daily_limits
        cap = limits.get(signal.symbol)
        profile_id = (
            str(signal.monitoring_profile_id) if signal.monitoring_profile_id is not None else None
        )
        if not self.symbol_daily_limiter.allow(
            signal.symbol,
            exchange=signal.exchange,
            profile_id=profile_id,
            limit=cap,
            now=signal.detected_at,
        ):
            metrics.signals_daily_limited_total += 1
            log(
                logger,
                logging.INFO,
                f"Daily limit reached for {signal.exchange}:{signal.symbol} "
                f"(max={cap})",
                component="app_state",
                event="signal_daily_limited",
                symbol=signal.symbol,
                signal_id=str(signal.id),
                signal_type=signal.signal_type.value,
            )
            return
        log(
            logger,
            logging.DEBUG,
            f"Dispatch signal {signal.exchange} {signal.symbol} "
            f"{signal.signal_type.value} id={signal.id}",
            component="app_state",
            event="dispatch_signal",
            symbol=signal.symbol,
            signal_id=str(signal.id),
            signal_type=signal.signal_type.value,
        )
        self.persistence.enqueue_signal(signal)
        if signal.exchange in self.admin_exchanges:
            self.notifications.enqueue(signal)
        if to_trading and self.trading is not None:
            self.trading.enqueue(signal)
        if cap is not None:
            self.symbol_daily_limiter.record(
                signal.symbol,
                signal.exchange,
                profile_id=profile_id,
                now=signal.detected_at,
            )

    async def rebuild_profile_monitors(self) -> None:
        """Reload all monitoring profiles into every feed's ProfileMonitor."""
        if self.profiles is None or not self.profile_monitors:
            return
        items = await self.profiles.list_profiles()
        system = self.signal_engine.settings if self.signal_engine is not None else ScreenerSettings()
        for monitor in self.profile_monitors:
            monitor.rebuild(items, system)
        log(
            logger,
            logging.INFO,
            f"Profile monitors rebuilt: {len(items)} profile(s), "
            f"{len(self.profile_monitors)} feed(s)",
            component="app_state",
            event="profile_monitors_rebuilt",
            count=len(items),
        )

    async def set_admin_exchanges(self, exchanges: set[str]) -> None:
        self.admin_exchanges = set(exchanges)
        for code, feed in self.feeds.items():
            if getattr(feed, "primary", False):
                continue
            feed.evaluate_admin = code in self.admin_exchanges
        if self.repo is not None:
            await self.repo.set_app_setting(
                "admin_exchanges", {"exchanges": sorted(exchanges)}
            )

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
        engines = [self.signal_engine]
        correlators = [self.correlator]
        registries = [self.registry]
        histories = []
        own_history = getattr(self, "history", None)
        if own_history is not None:
            histories.append(own_history)
        for feed in getattr(self, "feeds", {}).values():
            if feed.signal_engine is not None and feed.signal_engine not in engines:
                engines.append(feed.signal_engine)
            if feed.correlator is not None and feed.correlator not in correlators:
                correlators.append(feed.correlator)
            if feed.registry is not None and feed.registry not in registries:
                registries.append(feed.registry)
            feed_history = getattr(feed, "history", None)
            if feed_history is not None and feed_history not in histories:
                histories.append(feed_history)
        needed = history_capacity_for(
            max(
                settings.long_interval_sec,
                settings.short_interval_sec,
                settings.dump_interval_sec,
            )
        )
        for history in histories:
            ensure = getattr(history, "ensure_capacity", None)
            if callable(ensure):
                ensure(needed)
        for engine in engines:
            engine.update_settings(settings, context=active.context, reset_dedup=reset_dedup)
        for correlator in correlators:
            correlator.window_ms = settings.correlation_window_ms
        for registry in registries:
            registry.set_exclude_symbols(settings.exclude_symbols)

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
