from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from screener.domain.enums import (
    Direction,
    HistoryQuality,
    PriceEventKind,
    PriceSource,
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
    exchange: str = "bybit"


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


@dataclass(slots=True)
class TickerState:
    """Canonical merged Bybit ticker state for one symbol (snapshot + deltas)."""

    symbol: str
    last_price: float | None = None
    turnover_24h: float | None = None
    volume_24h: float | None = None
    mark_price: float | None = None
    index_price: float | None = None
    bid1_price: float | None = None
    ask1_price: float | None = None
    open_interest: float | None = None
    funding_rate: float | None = None
    cs: int | None = None
    exchange_ts_ms: int | None = None
    has_snapshot: bool = False
    last_price_updated_mono: float | None = None
    market_state_updated_mono: float | None = None

    def reset_snapshot(self) -> None:
        self.has_snapshot = False
        self.cs = None

    def apply(
        self,
        data: dict[str, Any],
        *,
        msg_type: str,
        ts_ms: int,
        cs: int | None,
        mono: float,
    ) -> bool | None:
        """Merge a snapshot or delta.

        Returns ``None`` when the message was not applied (delta before snapshot,
        out-of-order cs), otherwise whether ``lastPrice`` changed.
        """
        if msg_type == "snapshot":
            self.has_snapshot = True
        elif not self.has_snapshot:
            return None
        elif cs is not None and self.cs is not None and cs < self.cs:
            return None

        changed = False
        if "lastPrice" in data:
            price = _to_float(data["lastPrice"])
            if price is not None and price > 0 and price != self.last_price:
                self.last_price = price
                self.last_price_updated_mono = mono
                changed = True
        if "turnover24h" in data:
            self.turnover_24h = _to_float(data["turnover24h"])
        if "volume24h" in data:
            self.volume_24h = _to_float(data["volume24h"])
        if "markPrice" in data:
            self.mark_price = _to_float(data["markPrice"])
        if "indexPrice" in data:
            self.index_price = _to_float(data["indexPrice"])
        if "bid1Price" in data:
            self.bid1_price = _to_float(data["bid1Price"])
        if "ask1Price" in data:
            self.ask1_price = _to_float(data["ask1Price"])
        if "openInterest" in data:
            self.open_interest = _to_float(data["openInterest"])
        if "fundingRate" in data:
            self.funding_rate = _to_float(data["fundingRate"])

        self.exchange_ts_ms = ts_ms
        self.market_state_updated_mono = mono
        if cs is not None:
            self.cs = cs
        return changed


@dataclass(slots=True, frozen=True)
class PriceEvent:
    """One lastPrice observation. Every live event is processed, in order."""

    symbol: str
    kind: PriceEventKind
    last_price: float
    exchange_ts_ms: int
    received_mono: float  # time.perf_counter(): processing-latency clock only
    turnover_24h: float = 0.0
    volume_24h: float = 0.0


@dataclass(slots=True, frozen=True)
class RuleMatch:
    signal_type: SignalType
    interval_sec: int
    threshold_pct: float
    change_pct: float
    prev_price: float
    price: float
    detected_ts_ms: int
    history_quality: HistoryQuality
    level: int = 1

    def to_payload(self) -> dict[str, Any]:
        return {
            "signal_type": self.signal_type.value,
            "interval_sec": self.interval_sec,
            "threshold_pct": self.threshold_pct,
            "change_pct": self.change_pct,
            "prev_price": self.prev_price,
            "price": self.price,
            "detected_ts_ms": self.detected_ts_ms,
            "history_quality": self.history_quality.value,
            "level": self.level,
        }


@dataclass(slots=True, frozen=True)
class SignalContext:
    """Monitoring configuration that produced a match; frozen at evaluation time."""

    rule_version: str
    profile_id: UUID | None = None
    profile_version_id: UUID | None = None
    profile_name: str | None = None
    profile_version_no: int | None = None


@dataclass(slots=True, frozen=True)
class MarketSignal:
    """One user-facing market event: all correlated rule matches of a move."""

    symbol: str
    direction: Direction
    primary: RuleMatch
    matches: tuple[RuleMatch, ...]
    detected_at: datetime
    rule_version: str
    turnover_24h: float | None = None
    volume_24h: float | None = None
    price_source: PriceSource = PriceSource.LAST
    exchange: str = "bybit"
    category: str = "linear"
    id: UUID = field(default_factory=uuid4)
    monitoring_profile_id: UUID | None = None
    monitoring_profile_version_id: UUID | None = None
    monitoring_profile_name: str | None = None
    monitoring_profile_version_no: int | None = None

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
            "symbol": self.symbol,
            "direction": self.direction.value,
            "signal_type": self.signal_type.value,
            "interval_sec": self.interval_sec,
            "change_pct": self.change_pct,
            "price": self.price,
            "prev_price": self.prev_price,
            "detected_at": self.detected_at.isoformat(),
            "rule_version": self.rule_version,
            "turnover_24h": self.turnover_24h,
            "volume_24h": self.volume_24h,
            "price_source": self.price_source.value,
            "exchange": self.exchange,
            "category": self.category,
            "history_quality": self.history_quality.value,
            "level": self.level,
            "matches": [m.to_payload() for m in self.matches],
            "monitoring_profile": {
                "id": _opt_str(self.monitoring_profile_id),
                "version_id": _opt_str(self.monitoring_profile_version_id),
                "version_no": self.monitoring_profile_version_no,
                "name": self.monitoring_profile_name,
                "config_hash": self.rule_version,
            },
        }


def _opt_str(value: UUID | None) -> str | None:
    return None if value is None else str(value)
