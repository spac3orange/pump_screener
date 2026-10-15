from __future__ import annotations

import logging
from datetime import timedelta
from enum import StrEnum

from screener.application.clock import Clock
from screener.application.commercial.user_service import UserService
from screener.application.metrics import metrics
from screener.domain.commercial import UserAccount

logger = logging.getLogger(__name__)


class TrialResult(StrEnum):
    STARTED = "started"
    ALREADY_USED = "already_used"
    ALREADY_PAID = "already_paid"
    TERMS_REQUIRED = "terms_required"
    NOT_AVAILABLE = "not_available"


class TrialService:
    """One explicit trial per Telegram account; the DB update is the only arbiter."""

    def __init__(self, users: UserService, store, clock: Clock) -> None:
        self._users = users
        self._store = store
        self._clock = clock

    async def start(self, user: UserAccount) -> tuple[TrialResult, UserAccount]:
        settings = self._users.settings
        if user.is_admin:
            return TrialResult.NOT_AVAILABLE, user
        if not user.terms_accepted(settings.terms_version):
            return TrialResult.TERMS_REQUIRED, user
        if user.trial_used:
            return TrialResult.ALREADY_USED, user
        if await self._store.has_paid(user.id):
            return TrialResult.ALREADY_PAID, user
        updated = await self._store.start_trial(
            user.id,
            now=self._clock.now(),
            duration=timedelta(hours=settings.trial_duration_hours),
            grace=self._users.grace,
        )
        if updated is None:
            # Lost a race with a concurrent tap or a payment; re-read the truth.
            fresh = await self._users.get(user.id) or user
            if await self._store.has_paid(user.id):
                return TrialResult.ALREADY_PAID, fresh
            return TrialResult.ALREADY_USED, fresh
        metrics.trials_started_total += 1
        logger.info("Trial started user_id=%s", user.id)
        await self._users.apply_account(updated)
        return TrialResult.STARTED, updated
