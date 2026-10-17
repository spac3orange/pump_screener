from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Any, Literal

from pydantic import AliasChoices, BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

TRADFI_SYMBOL_TYPES = frozenset({"stock", "ETF", "commodity", "forex"})
MAX_RULE_INTERVAL_SEC = 3600
# Bump when ScreenerSettings fields change; stored profile versions carry it.
MONITORING_CONFIG_SCHEMA_VERSION = 1


class ScreenerSettings(BaseModel):
    """Complete monitoring configuration; one snapshot per monitoring profile version."""

    enable_long: bool = True
    long_threshold_pct: float = Field(default=40.0, gt=0)
    long_interval_sec: int = 720
    enable_short: bool = True
    short_threshold_pct: float = Field(default=10.0, gt=0)
    short_interval_sec: int = 1800
    enable_dump: bool = True
    dump_threshold_pct: float = Field(default=40.0, gt=0)
    dump_interval_sec: int = 900
    min_turnover_24h_usdt: float = Field(default=100_000.0, ge=0)
    cooldown_sec: int = Field(default=300, ge=0)
    escalate_step_pct: float = Field(default=2.0, gt=0)
    reset_ratio: float = Field(default=0.5, gt=0, le=1)
    reset_hold_sec: int = Field(default=60, ge=0)
    exclude_symbols: set[str] = Field(default_factory=set)
    # Allow signals whose reference point came from REST kline warmup/backfill.
    allow_mixed_history: bool = True
    # Rule matches of one symbol+direction within this window become one alert.
    correlation_window_ms: int = Field(default=400, ge=0, le=5000)
    # Price events processed later than this are counted as late (still evaluated).
    max_tick_age_ms: int = Field(default=5000, ge=500)

    @field_validator("long_interval_sec", "short_interval_sec", "dump_interval_sec")
    @classmethod
    def _validate_rule_interval(cls, value: int) -> int:
        if value < 60 or value > MAX_RULE_INTERVAL_SEC or value % 60 != 0:
            raise ValueError("interval must be a whole number of minutes from 1 to 60")
        return value

    @field_validator("exclude_symbols", mode="before")
    @classmethod
    def _parse_exclude(cls, value: object) -> set[str]:
        if value is None or value == "":
            return set()
        if isinstance(value, str):
            return {s.strip().upper() for s in value.split(",") if s.strip()}
        if isinstance(value, (list, set, tuple)):
            return {str(s).strip().upper() for s in value if str(s).strip()}
        raise TypeError("exclude_symbols must be a comma-separated string or collection")

    def canonical_config(self) -> dict[str, Any]:
        """Complete JSON-safe snapshot with a deterministic field/value order."""
        data = self.model_dump(mode="json")
        data["exclude_symbols"] = sorted(self.exclude_symbols)
        return data

    def config_hash(self) -> str:
        import hashlib
        import json

        raw = json.dumps(self.canonical_config(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()

    def rule_version(self) -> str:
        # Stable short fingerprint for signal provenance.
        return self.config_hash()[:12]

    @classmethod
    def from_profile_config(cls, config: dict[str, Any], schema_version: int) -> ScreenerSettings:
        """Validate a stored profile snapshot; profiles are complete, never overlays."""
        if schema_version != MONITORING_CONFIG_SCHEMA_VERSION:
            raise ValueError(f"unsupported monitoring config schema v{schema_version}")
        missing = set(cls.model_fields) - set(config)
        if missing:
            raise ValueError(f"incomplete profile config, missing: {', '.join(sorted(missing))}")
        unknown = set(config) - set(cls.model_fields)
        if unknown:
            raise ValueError(f"unknown profile config fields: {', '.join(sorted(unknown))}")
        return cls.model_validate(config)


class SystemSettings(BaseSettings):
    """Infrastructure / process configuration (env only)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "INFO"
    log_json: bool = True

    database_url: str = "postgresql+asyncpg://screener:screener@localhost:5432/screener"

    bybit_rest_base: str = "https://api.bybit.com"
    bybit_ws_linear: str = "wss://stream.bybit.com/v5/public/linear"
    binance_enabled: bool = True
    binance_rest_base: str = "https://fapi.binance.com"
    binance_ws_market: str = "wss://fstream.binance.com/market/stream?streams=!ticker@arr"
    binance_kline_concurrency: int = Field(default=4, ge=1, le=20)
    ws_symbols_per_connection: int = Field(default=100, ge=10, le=500)
    instrument_refresh_sec: int = Field(default=300, ge=30)
    # Connection watchdog: no frame (data or pong) for this long => reconnect.
    stale_connection_sec: int = Field(default=30, ge=5)
    # Lossless price-event queue; when full the WS reader waits (TCP backpressure).
    market_queue_maxsize: int = Field(default=10_000, ge=100)
    ws_args_char_budget: int = Field(default=20_000, ge=1000)
    ws_ping_interval_sec: float = Field(default=10.0, ge=5)
    # permessage-deflate: "deflate" (library default) or "none".
    ws_compression: Literal["deflate", "none"] = "deflate"
    # Frames buffered per shard before the reader stops reading the socket (no drop).
    ws_max_queue: int = Field(default=1024, ge=16, le=4096)
    ws_ack_timeout_sec: float = Field(default=10.0, ge=1)
    kline_warmup_concurrency: int = Field(default=8, ge=1, le=20)
    kline_warmup_limit: int = Field(default=62, ge=10, le=1000)
    # Gaps after reconnect longer than this are restored from REST 1m klines.
    gap_backfill_min_sec: int = Field(default=60, ge=5)
    signal_count_db_timeout_sec: float = Field(default=2.0, gt=0)
    # false: the app refuses to start on a schema behind head; run `alembic upgrade head` manually.
    auto_migrate: bool = True

    telegram_bot_token: str = ""
    # Comma-separated Telegram user IDs allowed to use the bot and receive signals.
    admin_id: Annotated[list[int], NoDecode] = Field(
        default_factory=list,
        validation_alias=AliasChoices("ADMIN_ID", "TELEGRAM_ALLOWED_IDS", "admin_id"),
    )

    # Screener defaults mirrored from env for bootstrap
    enable_long: bool = True
    long_threshold_pct: float = 40.0
    long_interval_sec: int = 720
    enable_short: bool = True
    short_threshold_pct: float = 10.0
    short_interval_sec: int = 1800
    enable_dump: bool = True
    dump_threshold_pct: float = 40.0
    dump_interval_sec: int = 900
    min_turnover_24h_usdt: float = 100_000.0
    cooldown_sec: int = 300
    escalate_step_pct: float = 2.0
    reset_ratio: float = 0.5
    reset_hold_sec: int = 60
    exclude_symbols: Annotated[set[str], NoDecode] = Field(default_factory=set)
    allow_mixed_history: bool = True
    signal_correlation_ms: int = 400
    max_tick_age_ms: int = 5000

    @field_validator("admin_id", mode="before")
    @classmethod
    def _parse_admin_ids(cls, value: object) -> list[int]:
        if value is None or value == "":
            return []
        if isinstance(value, int):
            return [value]
        if isinstance(value, str):
            parts = [p.strip() for p in value.split(",") if p.strip()]
            return [int(p) for p in parts]
        if isinstance(value, (list, tuple)):
            return [int(v) for v in value]
        raise TypeError("ADMIN_ID must be a Telegram user id or comma-separated ids")

    def is_admin(self, user_id: int | None) -> bool:
        if user_id is None or not self.admin_id:
            return False
        return user_id in self.admin_id

    @field_validator("exclude_symbols", mode="before")
    @classmethod
    def _parse_exclude(cls, value: object) -> set[str]:
        if value is None or value == "":
            return set()
        if isinstance(value, str):
            return {s.strip().upper() for s in value.split(",") if s.strip()}
        if isinstance(value, (list, set, tuple)):
            return {str(s).strip().upper() for s in value if str(s).strip()}
        raise TypeError("exclude_symbols must be a comma-separated string")

    def to_screener_settings(self) -> ScreenerSettings:
        return ScreenerSettings(
            enable_long=self.enable_long,
            long_threshold_pct=self.long_threshold_pct,
            long_interval_sec=self.long_interval_sec,
            enable_short=self.enable_short,
            short_threshold_pct=self.short_threshold_pct,
            short_interval_sec=self.short_interval_sec,
            enable_dump=self.enable_dump,
            dump_threshold_pct=self.dump_threshold_pct,
            dump_interval_sec=self.dump_interval_sec,
            min_turnover_24h_usdt=self.min_turnover_24h_usdt,
            cooldown_sec=self.cooldown_sec,
            escalate_step_pct=self.escalate_step_pct,
            reset_ratio=self.reset_ratio,
            reset_hold_sec=self.reset_hold_sec,
            exclude_symbols=self.exclude_symbols,
            allow_mixed_history=self.allow_mixed_history,
            correlation_window_ms=self.signal_correlation_ms,
            max_tick_age_ms=self.max_tick_age_ms,
        )


@lru_cache(maxsize=1)
def get_system_settings() -> SystemSettings:
    return SystemSettings()


class TradingSettings(BaseSettings):
    """Server-side optional auto-trading policy (env only; never Telegram)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    bybit_trading_mode: str = "disabled"
    allow_mainnet_trading: bool = False
    bybit_api_key: str = ""
    bybit_api_secret: str = ""
    trading_auto_resume: bool = False

    pump_action: str = "SHORT"
    drop_action: str = "IGNORE"
    dump_action: str = "LONG"

    bybit_leverage: float = Field(default=3.0, gt=0)
    trade_margin_pct: float = Field(default=2.0, gt=0, le=100)
    order_sizing_price: str = "markPrice"
    entry_order_type: str = "MARKET"
    max_open_positions: int = Field(default=1, ge=1)
    existing_position_policy: str = "skip"

    stop_loss_enabled: bool = True
    stop_loss_pct: float = Field(default=3.0, gt=0)
    take_profit_enabled: bool = True
    take_profit_pct: float = Field(default=5.0, gt=0)
    tp_trigger_by: str = "MarkPrice"
    sl_trigger_by: str = "MarkPrice"

    max_signal_age_seconds: float = Field(default=30.0, gt=0)
    trading_notify_skips: bool = True
    trading_queue_maxsize: int = Field(default=1000, ge=10)
    instrument_cache_ttl_sec: float = Field(default=300.0, gt=0)
    account_stats_cache_ttl_sec: float = Field(default=3.0, gt=0)
    fill_timeout_sec: float = Field(default=30.0, gt=0)

    # Position lifecycle: close detection + Telegram close notifications.
    trading_notify_closes: bool = True
    lifecycle_reconcile_interval_sec: float = Field(default=45.0, ge=10, le=600)
    lifecycle_correlation_window_sec: float = Field(default=3.0, ge=0.5, le=30)
    lifecycle_pnl_wait_sec: float = Field(default=3.0, ge=0, le=30)

    @field_validator("bybit_trading_mode", mode="before")
    @classmethod
    def _mode(cls, value: object) -> str:
        text = str(value or "disabled").strip().lower()
        if text not in {"disabled", "demo", "mainnet"}:
            raise ValueError("BYBIT_TRADING_MODE must be disabled|demo|mainnet")
        return text

    @field_validator("pump_action", "drop_action", "dump_action", mode="before")
    @classmethod
    def _action(cls, value: object) -> str:
        text = str(value or "IGNORE").strip().upper()
        if text not in {"LONG", "SHORT", "IGNORE"}:
            raise ValueError("trade action must be LONG|SHORT|IGNORE")
        return text

    @field_validator("order_sizing_price", mode="before")
    @classmethod
    def _sizing_price(cls, value: object) -> str:
        text = str(value or "markPrice").strip()
        if text not in {"lastPrice", "markPrice"}:
            raise ValueError("ORDER_SIZING_PRICE must be lastPrice|markPrice")
        return text

    @field_validator("entry_order_type", mode="before")
    @classmethod
    def _entry(cls, value: object) -> str:
        text = str(value or "MARKET").strip().upper()
        if text != "MARKET":
            raise ValueError("ENTRY_ORDER_TYPE must be MARKET in v1")
        return text

    @field_validator("existing_position_policy", mode="before")
    @classmethod
    def _pos_policy(cls, value: object) -> str:
        text = str(value or "skip").strip().lower()
        if text != "skip":
            raise ValueError("EXISTING_POSITION_POLICY must be skip in v1")
        return text

    @property
    def enabled_module(self) -> bool:
        return self.bybit_trading_mode != "disabled"

    @property
    def is_demo(self) -> bool:
        return self.bybit_trading_mode == "demo"

    def action_for(self, signal_type: str) -> str:
        mapping = {
            "PUMP": self.pump_action,
            "DROP": self.drop_action,
            "DUMP": self.dump_action,
        }
        return mapping.get(signal_type.upper(), "IGNORE")

    def mainnet_orders_allowed(self) -> bool:
        return (
            self.bybit_trading_mode == "mainnet"
            and self.allow_mainnet_trading
            and bool(self.bybit_api_key)
            and bool(self.bybit_api_secret)
        )

    def orders_environment_ok(self) -> bool:
        if self.bybit_trading_mode == "disabled":
            return False
        if not self.bybit_api_key or not self.bybit_api_secret:
            return False
        if self.bybit_trading_mode == "mainnet":
            return self.allow_mainnet_trading
        return True


@lru_cache(maxsize=1)
def get_trading_settings() -> TradingSettings:
    return TradingSettings()


class CommercialSettings(BaseSettings):
    """Multi-user Trial / Telegram Stars subscription (env only)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # false: the bot stays admin-only exactly as before.
    commercial_enabled: bool = False
    trial_duration_hours: int = Field(default=24, ge=1, le=720)
    # Telegram caps subscription invoices at 10000 Stars.
    subscription_price_stars: int = Field(default=1500, ge=1, le=10_000)
    subscription_title: str = "PumpScreener Pro"
    # Access window kept past period end while auto-renewal is still on.
    subscription_renewal_grace_min: int = Field(default=15, ge=0, le=1440)
    invoice_ttl_hours: int = Field(default=24, ge=1, le=168)
    invoice_rate_limit_per_hour: int = Field(default=5, ge=1, le=100)
    terms_version: str = Field(default="1", min_length=1, max_length=16)
    terms_url: str = ""
    support_contact: str = ""

    user_signal_max_age_sec: int = Field(default=120, ge=5, le=3600)
    user_send_rate_per_sec: float = Field(default=25.0, gt=0, le=1000)
    user_chat_min_interval_sec: float = Field(default=1.0, ge=0, le=60)
    user_queue_maxsize: int = Field(default=50, ge=1, le=1000)
    fanout_queue_maxsize: int = Field(default=10_000, ge=100)
    expiry_check_sec: int = Field(default=30, ge=5, le=300)
    user_signal_retention_days: int = Field(default=30, ge=1, le=365)
    # Incoming updates per user; bursts above this are ignored silently.
    user_rate_per_sec: float = Field(default=3.0, gt=0, le=100)
    user_rate_burst: int = Field(default=10, ge=1, le=100)


@lru_cache(maxsize=1)
def get_commercial_settings() -> CommercialSettings:
    return CommercialSettings()
