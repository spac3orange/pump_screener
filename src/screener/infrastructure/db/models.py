from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class InstrumentRow(Base):
    __tablename__ = "instruments"

    exchange: Mapped[str] = mapped_column(String(16), primary_key=True, default="bybit")
    symbol: Mapped[str] = mapped_column(String(64), primary_key=True)
    contract_type: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(32), default="")
    settle_coin: Mapped[str] = mapped_column(String(16), default="")
    base_coin: Mapped[str] = mapped_column(String(32), default="")
    quote_coin: Mapped[str] = mapped_column(String(32), default="")
    symbol_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_pre_listing: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_instruments_status", "status"),
        Index("ix_instruments_updated_at", "updated_at"),
    )


class SignalRow(Base):
    __tablename__ = "signals"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    exchange: Mapped[str] = mapped_column(String(16), nullable=False, server_default="bybit")
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    signal_type: Mapped[str] = mapped_column(String(32), nullable=False)
    interval_sec: Mapped[int] = mapped_column(Integer, nullable=False)
    change_pct: Mapped[float] = mapped_column(Float, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    prev_price: Mapped[float] = mapped_column(Float, nullable=False)
    turnover_24h: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    rule_version: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # NULL for signals created before monitoring profiles existed.
    monitoring_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    monitoring_profile_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("monitoring_profile_versions.id"),
        nullable=True,
    )

    notifications: Mapped[list[NotificationRow]] = relationship(back_populates="signal")

    __table_args__ = (
        Index("ix_signals_detected_at", "detected_at"),
        Index("ix_signals_exchange_symbol_detected", "exchange", "symbol", "detected_at"),
        Index("ix_signals_symbol_type_detected", "symbol", "signal_type", "detected_at"),
        Index("ix_signals_type_detected", "signal_type", "detected_at"),
        Index("ix_signals_profile_version", "monitoring_profile_version_id"),
    )


class MonitoringProfileRow(Base):
    __tablename__ = "monitoring_profiles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "monitoring_profile_versions.id",
            use_alter=True,
            name="fk_monitoring_profiles_current_version",
        ),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    # Soft delete keeps versions referenced by historical signals.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


Index(
    "uq_monitoring_profiles_name_ci",
    func.lower(MonitoringProfileRow.name),
    unique=True,
    postgresql_where=MonitoringProfileRow.deleted_at.is_(None),
)


class MonitoringProfileVersionRow(Base):
    """Immutable config snapshot; every save of a profile appends one row."""

    __tablename__ = "monitoring_profile_versions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("monitoring_profiles.id"), nullable=False
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    config_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_by_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (
        Index("uq_monitoring_profile_versions_no", "profile_id", "version_no", unique=True),
    )


class MonitoringRuntimeStateRow(Base):
    """Single row (id=1): exactly one active monitoring profile."""

    __tablename__ = "monitoring_runtime_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    active_profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("monitoring_profiles.id"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    updated_by_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class ScreenerSettingsRow(Base):
    """Deprecated: pre-profile single config row, read once to seed ``Default``."""

    __tablename__ = "screener_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    updated_by_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class NotificationRow(Base):
    __tablename__ = "notification_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("signals.id", ondelete="CASCADE"), nullable=False
    )
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    signal: Mapped[SignalRow] = relationship(back_populates="notifications")

    __table_args__ = (
        Index("ix_notification_signal_id", "signal_id"),
        Index("ix_notification_status_sent", "status", "sent_at"),
    )


class SystemEventRow(Base):
    __tablename__ = "system_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    level: Mapped[str] = mapped_column(String(16), nullable=False)
    component: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_system_events_created_at", "created_at"),
        Index("ix_system_events_component_created", "component", "created_at"),
    )


class TradingExecutionRow(Base):
    __tablename__ = "trading_executions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    signal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, unique=True)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    signal_type: Mapped[str] = mapped_column(String(32), nullable=False)
    configured_action: Mapped[str] = mapped_column(String(16), nullable=False)
    side: Mapped[str | None] = mapped_column(String(8), nullable=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    order_link_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    bybit_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    requested_qty: Mapped[float | None] = mapped_column(Float, nullable=True)
    filled_qty: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    leverage: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    skip_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    # Position lifecycle (migration 005). Money values come from Bybit executions as-is.
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    remaining_qty: Mapped[Decimal | None] = mapped_column(Numeric(38, 12), nullable=True)
    tp_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sl_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    close_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    close_reason_detail: Mapped[str | None] = mapped_column(String(200), nullable=True)
    exit_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 12), nullable=True)
    closed_qty: Mapped[Decimal | None] = mapped_column(Numeric(38, 12), nullable=True)
    realized_pnl: Mapped[Decimal | None] = mapped_column(Numeric(38, 12), nullable=True)
    trading_fee: Mapped[Decimal | None] = mapped_column(Numeric(38, 12), nullable=True)
    funding_fee: Mapped[Decimal | None] = mapped_column(Numeric(38, 12), nullable=True)
    net_pnl: Mapped[Decimal | None] = mapped_column(Numeric(38, 12), nullable=True)
    bybit_closed_pnl: Mapped[Decimal | None] = mapped_column(Numeric(38, 12), nullable=True)
    balance_after: Mapped[Decimal | None] = mapped_column(Numeric(38, 12), nullable=True)
    close_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    close_execution_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    close_recovered: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    close_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_trading_executions_symbol_created", "symbol", "created_at"),
        Index("ix_trading_executions_status_created", "status", "created_at"),
    )


class TradingRuntimeStateRow(Base):
    __tablename__ = "trading_runtime_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    updated_by_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


# --- Commercial (migration 006) ----------------------------------------------


class UserRow(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, server_default="user")
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    language_code: Mapped[str | None] = mapped_column(String(8), nullable=True)
    monitoring_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    terms_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    terms_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trial_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    access_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expiry_notified_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bot_blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    enabled_exchanges: Mapped[list[str]] = mapped_column(
        ARRAY(Text),
        nullable=False,
        server_default=text("ARRAY['bybit','binance']::text[]"),
    )


class UserProfileTemplateRow(Base):
    __tablename__ = "user_profile_templates"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    config_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_profile_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("monitoring_profile_versions.id"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class UserMonitoringProfileRow(Base):
    __tablename__ = "user_monitoring_profiles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_monitoring_profile_versions.id", use_alter=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class UserMonitoringProfileVersionRow(Base):
    """Immutable: a settings change always inserts a new version."""

    __tablename__ = "user_monitoring_profile_versions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_monitoring_profiles.id", ondelete="CASCADE"),
        nullable=False,
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    config_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PaymentInvoiceRow(Base):
    __tablename__ = "payment_invoices"

    payload: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    plan_code: Mapped[str] = mapped_column(String(32), nullable=False)
    amount_stars: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="created")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SubscriptionRow(Base):
    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    plan_code: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    invoice_payload: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    subscription_charge_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    current_period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cancel_at_period_end: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


Index(
    "uq_subscriptions_one_live_per_user",
    SubscriptionRow.user_id,
    unique=True,
    postgresql_where=SubscriptionRow.status.in_(("active", "canceled")),
)


class PaymentRow(Base):
    """Append-only ledger; ``(telegram_payment_charge_id, kind)`` is the idempotency key."""

    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"))
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscriptions.id")
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    invoice_payload: Mapped[str] = mapped_column(String(64), nullable=False)
    telegram_payment_charge_id: Mapped[str] = mapped_column(String(128), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    total_amount: Mapped[int] = mapped_column(Integer, nullable=False)
    is_recurring: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    is_first_recurring: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    subscription_expiration_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    anomaly_reason: Mapped[str | None] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("telegram_payment_charge_id", "kind", name="uq_payments_charge_kind"),
    )


class CommercialSignalRow(Base):
    __tablename__ = "commercial_signals"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    group_key: Mapped[str] = mapped_column(String(64), nullable=False)
    exchange: Mapped[str] = mapped_column(String(16), nullable=False, server_default="bybit")
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    signal_type: Mapped[str] = mapped_column(String(32), nullable=False)
    interval_sec: Mapped[int] = mapped_column(Integer, nullable=False)
    change_pct: Mapped[float] = mapped_column(Float, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    prev_price: Mapped[float] = mapped_column(Float, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index(
            "ix_commercial_signals_exchange_symbol_detected",
            "exchange",
            "symbol",
            "detected_at",
        ),
    )


class AppSettingsRow(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class UserSignalEventRow(Base):
    __tablename__ = "user_signal_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    signal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("commercial_signals.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(32))
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("user_id", "signal_id", name="uq_user_signal_events_user_signal"),
    )
