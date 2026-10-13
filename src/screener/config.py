from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import AliasChoices, BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

TRADFI_SYMBOL_TYPES = frozenset({"stock", "ETF", "commodity", "forex"})
MAX_RULE_INTERVAL_SEC = 3600


class ScreenerSettings(BaseModel):
    """User-tunable screener rules (env defaults + DB overlay)."""

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

    def rule_version(self) -> str:
        # Stable short fingerprint for signal provenance.
        import hashlib
        import json

        raw = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()[:12]


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
    ws_symbols_per_connection: int = Field(default=100, ge=10, le=500)
    instrument_refresh_sec: int = Field(default=300, ge=30)
    # Connection watchdog: no frame (data or pong) for this long => reconnect.
    stale_connection_sec: int = Field(default=30, ge=5)
    # Lossless price-event queue; when full the WS reader waits (TCP backpressure).
    market_queue_maxsize: int = Field(default=10_000, ge=100)
    ws_args_char_budget: int = Field(default=20_000, ge=1000)
    ws_ping_interval_sec: float = Field(default=10.0, ge=5)
    ws_ack_timeout_sec: float = Field(default=10.0, ge=1)
    kline_warmup_concurrency: int = Field(default=8, ge=1, le=20)
    kline_warmup_limit: int = Field(default=62, ge=10, le=1000)
    # Gaps after reconnect longer than this are restored from REST 1m klines.
    gap_backfill_min_sec: int = Field(default=60, ge=5)
    signal_count_db_timeout_sec: float = Field(default=2.0, gt=0)

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
