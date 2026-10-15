"""Commercial (multi-user) domain: accounts, access, subscriptions, user signals.

Nothing here references the trading module: commercial signals are a separate
type (``GroupSignal``) that cannot be passed to ``TradingService.enqueue``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator

from screener.config import MAX_RULE_INTERVAL_SEC, ScreenerSettings
from screener.domain.enums import Direction, HistoryQuality, SignalType
from screener.domain.models import RuleMatch

# Telegram Stars subscriptions currently support exactly one period: 30 days.
SUBSCRIPTION_PERIOD_SEC = 2_592_000
STARS_CURRENCY = "XTR"
PLAN_PRO_30D = "pro_30d"
USER_CONFIG_SCHEMA_VERSION = 1
DEFAULT_TEMPLATE = "default"


class UserRole(StrEnum):
    USER = "user"
    ADMIN = "admin"


class UserStatus(StrEnum):
    ACTIVE = "active"
    BOT_BLOCKED = "bot_blocked"
    BANNED = "banned"


class AccessKind(StrEnum):
    NONE = "none"
    TRIAL = "trial"
    PRO = "pro"
    ADMIN = "admin"


class SubscriptionStatus(StrEnum):
    ACTIVE = "active"
    CANCELED = "canceled"
    EXPIRED = "expired"
    REFUNDED = "refunded"


LIVE_SUBSCRIPTION_STATUSES = (SubscriptionStatus.ACTIVE, SubscriptionStatus.CANCELED)


class InvoiceStatus(StrEnum):
    CREATED = "created"
    PRE_CHECKOUT_OK = "pre_checkout_ok"
    PAID = "paid"
    EXPIRED = "expired"


class PaymentKind(StrEnum):
    INITIAL = "initial"
    RENEWAL = "renewal"
    REFUND = "refund"


class PaymentStatus(StrEnum):
    APPLIED = "applied"
    ANOMALY = "anomaly"


class DeliveryOutcome(StrEnum):
    SENT = "sent"
    FAILED = "failed"
    DROPPED_EXPIRED = "dropped_expired"
    DROPPED_ACCESS = "dropped_access"
    DROPPED_OVERFLOW = "dropped_overflow"
    DROPPED_BLOCKED = "dropped_blocked"


_RULE_INTERVAL_FIELDS = ("long_interval_sec", "short_interval_sec", "dump_interval_sec")


class UserDetectionConfig(BaseModel):
    """The part of the monitoring config a subscriber may change.

    Dedup, correlation, history and exclusion parameters stay server-side and
    come from the template, so they are identical for every user.
    """

    enable_long: bool = True
    long_threshold_pct: float = Field(default=40.0, gt=0, le=500)
    long_interval_sec: int = 720
    enable_short: bool = True
    short_threshold_pct: float = Field(default=10.0, gt=0, le=500)
    short_interval_sec: int = 1800
    enable_dump: bool = True
    dump_threshold_pct: float = Field(default=40.0, gt=0, le=500)
    dump_interval_sec: int = 900
    min_turnover_24h_usdt: float = Field(default=100_000.0, ge=0, le=1e12)

    @field_validator(*_RULE_INTERVAL_FIELDS)
    @classmethod
    def _validate_rule_interval(cls, value: int) -> int:
        if value < 60 or value > MAX_RULE_INTERVAL_SEC or value % 60 != 0:
            raise ValueError("interval must be a whole number of minutes from 1 to 60")
        return value

    @classmethod
    def from_settings(cls, settings: ScreenerSettings) -> UserDetectionConfig:
        return cls.model_validate({name: getattr(settings, name) for name in cls.model_fields})

    @classmethod
    def from_stored(cls, config: dict[str, Any], schema_version: int) -> UserDetectionConfig:
        if schema_version != USER_CONFIG_SCHEMA_VERSION:
            raise ValueError(f"unsupported user config schema v{schema_version}")
        return cls.model_validate(config)

    def to_settings(self, system: ScreenerSettings) -> ScreenerSettings:
        """Complete effective settings: server-side parameters + this user's rules."""
        return system.model_copy(update=self.model_dump())


@dataclass(slots=True, frozen=True)
class UserAccount:
    id: int
    telegram_user_id: int
    chat_id: int
    role: UserRole
    status: UserStatus
    monitoring_enabled: bool
    terms_version: str | None
    terms_accepted_at: datetime | None
    trial_started_at: datetime | None
    trial_ends_at: datetime | None
    access_until: datetime | None
    expiry_notified_until: datetime | None
    created_at: datetime | None = None
    language_code: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.role is UserRole.ADMIN

    @property
    def trial_used(self) -> bool:
        return self.trial_started_at is not None

    def terms_accepted(self, version: str) -> bool:
        return self.terms_accepted_at is not None and self.terms_version == version


@dataclass(slots=True, frozen=True)
class SubscriptionInfo:
    id: UUID
    user_id: int
    plan_code: str
    status: SubscriptionStatus
    invoice_payload: str
    subscription_charge_id: str
    current_period_end: datetime
    cancel_at_period_end: bool
    started_at: datetime | None = None

    @property
    def is_live(self) -> bool:
        return self.status in LIVE_SUBSCRIPTION_STATUSES


@dataclass(slots=True, frozen=True)
class InvoiceInfo:
    payload: str
    user_id: int
    plan_code: str
    amount_stars: int
    currency: str
    status: InvoiceStatus
    created_at: datetime
    expires_at: datetime


@dataclass(slots=True, frozen=True)
class AccessState:
    kind: AccessKind
    until: datetime | None = None

    @property
    def active(self) -> bool:
        return self.kind is not AccessKind.NONE


@dataclass(slots=True, frozen=True)
class PaymentInput:
    """Fields of Telegram ``SuccessfulPayment`` that drive access (nothing else is kept)."""

    telegram_user_id: int
    currency: str
    total_amount: int
    invoice_payload: str
    telegram_payment_charge_id: str
    is_recurring: bool = False
    is_first_recurring: bool = False
    subscription_expiration_date: datetime | None = None

    @property
    def kind(self) -> PaymentKind:
        if self.is_recurring and not self.is_first_recurring:
            return PaymentKind.RENEWAL
        return PaymentKind.INITIAL


@dataclass(slots=True, frozen=True)
class UserSignalRecord:
    """Outcome of one signal for one user (``user_signal_events`` row)."""

    user_id: int
    signal_id: UUID
    status: str
    error_code: str | None
    attempts: int
    created_at: datetime
    sent_at: datetime | None


@dataclass(slots=True, frozen=True)
class GroupSignal:
    """A market signal for every subscriber whose config hashes to ``group_key``."""

    group_key: str
    symbol: str
    direction: Direction
    primary: RuleMatch
    matches: tuple[RuleMatch, ...]
    detected_at: datetime
    turnover_24h: float | None = None
    id: UUID = field(default_factory=uuid4)

    @property
    def signal_type(self) -> SignalType:
        return self.primary.signal_type

    @property
    def interval_sec(self) -> int:
        return self.primary.interval_sec

    @property
    def change_pct(self) -> float:
        return self.primary.change_pct

    @property
    def price(self) -> float:
        return self.primary.price

    @property
    def prev_price(self) -> float:
        return self.primary.prev_price

    @property
    def history_quality(self) -> HistoryQuality:
        return self.primary.history_quality

    @property
    def level(self) -> int:
        return max(m.level for m in self.matches)

    def secondary(self) -> list[RuleMatch]:
        return [m for m in self.matches if m is not self.primary]

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "group_key": self.group_key,
            "symbol": self.symbol,
            "direction": self.direction.value,
            "signal_type": self.signal_type.value,
            "interval_sec": self.interval_sec,
            "change_pct": self.change_pct,
            "price": self.price,
            "prev_price": self.prev_price,
            "detected_at": self.detected_at.isoformat(),
            "turnover_24h": self.turnover_24h,
            "history_quality": self.history_quality.value,
            "level": self.level,
            "matches": [m.to_payload() for m in self.matches],
        }
