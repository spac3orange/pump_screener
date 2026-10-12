from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from screener.domain.enums import (
    HistoryQuality,
    HistorySource,
    PriceSource,
    SideHint,
    SignalType,
)


@dataclass(slots=True, frozen=True)
class Instrument:
    symbol: str
    contract_type: str
    status: str
    settle_coin: str
    base_coin: str
    quote_coin: str
    symbol_type: str | None
    is_pre_listing: bool


@dataclass(slots=True)
class TickerState:
    symbol: str
    last_price: float | None = None
    turnover_24h: float | None = None
    volume_24h: float | None = None
    mark_price: float | None = None
    index_price: float | None = None
    cs: int | None = None
    exchange_ts_ms: int | None = None
    received_ts_ms: int | None = None
    has_snapshot: bool = False

    def apply_fields(self, data: dict[str, Any], *, msg_type: str, ts_ms: int, cs: int | None) -> None:
        if msg_type == "snapshot":
            self.has_snapshot = True
        elif not self.has_snapshot:
            # Ignore delta until first snapshot after (re)subscribe.
            return

        if cs is not None and self.cs is not None and cs < self.cs:
            return

        def _f(key: str) -> float | None:
            if key not in data or data[key] in (None, ""):
                return None
            return float(data[key])

        if "lastPrice" in data:
            self.last_price = _f("lastPrice")
        if "turnover24h" in data:
            self.turnover_24h = _f("turnover24h")
        if "volume24h" in data:
            self.volume_24h = _f("volume24h")
        if "markPrice" in data:
            self.mark_price = _f("markPrice")
        if "indexPrice" in data:
            self.index_price = _f("indexPrice")

        self.exchange_ts_ms = ts_ms
        if cs is not None:
            self.cs = cs


@dataclass(slots=True, frozen=True)
class MarketTick:
    symbol: str
    last_price: float
    turnover_24h: float
    volume_24h: float
    exchange_ts_ms: int
    received_ts_ms: int
    source: HistorySource = HistorySource.TICKER


@dataclass(slots=True, frozen=True)
class HistoryPoint:
    ts_ms: int
    last_price: float
    turnover_24h: float
    source: HistorySource


@dataclass(slots=True, frozen=True)
class ConditionResult:
    name: str
    passed: bool
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class SignalEvent:
    symbol: str
    signal_type: SignalType
    side_hint: SideHint
    interval_sec: int
    change_pct: float
    price: float
    prev_price: float
    detected_at: datetime
    rule_version: str
    turnover_24h: float | None = None
    volume_24h: float | None = None
    volume_ratio: float | None = None
    price_source: PriceSource = PriceSource.LAST
    exchange: str = "bybit"
    category: str = "linear"
    history_quality: HistoryQuality = HistoryQuality.LIVE
    level: int = 1
    conditions: list[ConditionResult] = field(default_factory=list)
    id: UUID = field(default_factory=uuid4)

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "symbol": self.symbol,
            "signal_type": self.signal_type.value,
            "side_hint": self.side_hint.value,
            "interval_sec": self.interval_sec,
            "change_pct": self.change_pct,
            "price": self.price,
            "prev_price": self.prev_price,
            "detected_at": self.detected_at.isoformat(),
            "rule_version": self.rule_version,
            "turnover_24h": self.turnover_24h,
            "volume_24h": self.volume_24h,
            "volume_ratio": self.volume_ratio,
            "price_source": self.price_source.value,
            "exchange": self.exchange,
            "category": self.category,
            "history_quality": self.history_quality.value,
            "level": self.level,
            "conditions": [
                {"name": c.name, "passed": c.passed, "detail": c.detail} for c in self.conditions
            ],
        }
