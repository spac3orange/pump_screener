"""Wiring of the commercial (multi-user) subsystem.

Commercial signals flow MultiConfigEngine -> GroupCorrelator -> UserDispatcher
and never reach ``AppState.dispatch_signal`` or the trading module. Each
monitored exchange has its own engine/correlator on its own PriceHistory.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from screener.application.clock import Clock, SystemClock
from screener.application.commercial.dispatcher import FormatSignal, UserDispatcher
from screener.application.commercial.expiration_worker import ExpirationWorker
from screener.application.commercial.group_correlator import GroupCorrelator
from screener.application.commercial.multi_engine import MultiConfigEngine
from screener.application.commercial.payment_service import PaymentService
from screener.application.commercial.rate_limit import UserRateLimiter
from screener.application.commercial.registry import ActiveUserRegistry
from screener.application.commercial.signal_writer import CommercialSignalWriter
from screener.application.commercial.trial_service import TrialService
from screener.application.commercial.user_service import UserService
from screener.application.price_history import PriceHistoryManager
from screener.config import CommercialSettings, ScreenerSettings
from screener.domain.exchanges import BYBIT
from screener.infrastructure.db.commercial_repo import CommercialRepository

logger = logging.getLogger(__name__)


@dataclass
class CommercialRuntime:
    settings: CommercialSettings
    repo: CommercialRepository
    registry: ActiveUserRegistry
    engine: MultiConfigEngine
    correlator: GroupCorrelator
    writer: CommercialSignalWriter
    dispatcher: UserDispatcher
    users: UserService
    trials: TrialService
    payments: PaymentService
    expiry: ExpirationWorker
    rate_limiter: UserRateLimiter
    engines: dict[str, MultiConfigEngine] = field(default_factory=dict)
    correlators: dict[str, GroupCorrelator] = field(default_factory=dict)

    async def start(self) -> None:
        await self.writer.start()
        await self.dispatcher.start()
        await self.expiry.start()

    async def stop(self) -> None:
        await self.expiry.stop()
        for correlator in self.correlators.values():
            correlator.flush_all()
        await self.dispatcher.stop()
        await self.writer.stop()

    def apply_groups(self, template: ScreenerSettings) -> None:
        for exchange, engine in self.engines.items():
            engine.set_groups(self.registry.groups_for(exchange), template)


def build_commercial(
    *,
    settings: CommercialSettings,
    session_factory: async_sessionmaker[AsyncSession],
    history: PriceHistoryManager,
    template_hint: ScreenerSettings,
    format_signal: FormatSignal,
    clock: Clock | None = None,
    histories: Mapping[str, PriceHistoryManager] | None = None,
) -> CommercialRuntime:
    """Build the object graph; ``UserService.bootstrap`` loads users afterwards."""
    clock = clock or SystemClock()
    repo = CommercialRepository(session_factory)
    feeds = dict(histories) if histories else {BYBIT: history}
    dispatcher_holder: dict[str, UserDispatcher] = {}
    engines: dict[str, MultiConfigEngine] = {}
    correlators: dict[str, GroupCorrelator] = {}
    for exchange, hist in feeds.items():
        engine = MultiConfigEngine(hist, template_hint)
        correlator = GroupCorrelator(
            window_ms=template_hint.correlation_window_ms, exchange=exchange
        )
        engine.on_matches = correlator.add
        engines[exchange] = engine
        correlators[exchange] = correlator
    primary = engines.get(BYBIT) or next(iter(engines.values()))
    primary_corr = correlators.get(BYBIT) or next(iter(correlators.values()))
    writer = CommercialSignalWriter(repo)
    registry = ActiveUserRegistry()
    users = UserService(repo, registry, clock, settings)
    dispatcher = UserDispatcher(
        registry,
        writer,
        clock,
        settings,
        format_signal=format_signal,
    )
    dispatcher_holder["d"] = dispatcher
    for correlator in correlators.values():
        correlator.on_signal = dispatcher.on_group_signal

    def on_groups(_groups: dict[str, ScreenerSettings] | None = None) -> None:
        template = users._template or template_hint
        for exchange, engine in engines.items():
            engine.set_groups(registry.groups_for(exchange), template)

    registry.on_groups_changed = on_groups
    dispatcher.set_on_blocked(users.mark_blocked)
    return CommercialRuntime(
        settings=settings,
        repo=repo,
        registry=registry,
        engine=primary,
        correlator=primary_corr,
        writer=writer,
        dispatcher=dispatcher,
        users=users,
        trials=TrialService(users, repo, clock),
        payments=PaymentService(repo, users, None, clock),
        expiry=ExpirationWorker(repo, users, clock),
        rate_limiter=UserRateLimiter(
            rate_per_sec=settings.user_rate_per_sec, burst=settings.user_rate_burst
        ),
        engines=engines,
        correlators=correlators,
    )
