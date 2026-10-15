"""Wiring of the commercial (multi-user) subsystem.

Commercial signals flow MultiConfigEngine -> GroupCorrelator -> UserDispatcher
and never reach ``AppState.dispatch_signal`` or the trading module.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

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
from screener.infrastructure.db.commercial_repo import CommercialRepository

logger = logging.getLogger(__name__)

SIGNAL_FOOTER = "\n\n<i>PumpScreener Pro</i>"


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

    async def start(self) -> None:
        await self.writer.start()
        await self.dispatcher.start()
        await self.expiry.start()

    async def stop(self) -> None:
        await self.expiry.stop()
        self.correlator.flush_all()
        await self.dispatcher.stop()
        await self.writer.stop()


def build_commercial(
    *,
    settings: CommercialSettings,
    session_factory: async_sessionmaker[AsyncSession],
    history: PriceHistoryManager,
    template_hint: ScreenerSettings,
    format_signal: FormatSignal,
    clock: Clock | None = None,
) -> CommercialRuntime:
    """Build the object graph; ``UserService.bootstrap`` loads users afterwards."""
    clock = clock or SystemClock()
    repo = CommercialRepository(session_factory)
    engine = MultiConfigEngine(history, template_hint)
    correlator = GroupCorrelator(window_ms=template_hint.correlation_window_ms)
    engine.on_matches = correlator.add
    registry = ActiveUserRegistry(on_groups_changed=engine.set_groups)
    writer = CommercialSignalWriter(repo)
    dispatcher = UserDispatcher(
        registry,
        writer,
        clock,
        settings,
        format_signal=format_signal,
        footer=SIGNAL_FOOTER,
    )
    correlator.on_signal = dispatcher.on_group_signal
    users = UserService(repo, registry, clock, settings)
    dispatcher.set_on_blocked(users.mark_blocked)
    return CommercialRuntime(
        settings=settings,
        repo=repo,
        registry=registry,
        engine=engine,
        correlator=correlator,
        writer=writer,
        dispatcher=dispatcher,
        users=users,
        trials=TrialService(users, repo, clock),
        payments=PaymentService(repo, users, None, clock),
        expiry=ExpirationWorker(repo, users, clock),
        rate_limiter=UserRateLimiter(
            rate_per_sec=settings.user_rate_per_sec, burst=settings.user_rate_burst
        ),
    )
