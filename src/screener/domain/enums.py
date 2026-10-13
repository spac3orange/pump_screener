from __future__ import annotations

from enum import StrEnum


class SignalType(StrEnum):
    """Market event detected by a price rule. Not a trading decision."""

    PUMP = "PUMP"
    DROP = "DROP"
    DUMP = "DUMP"


class Direction(StrEnum):
    UP = "UP"
    DOWN = "DOWN"


class PriceSource(StrEnum):
    LAST = "lastPrice"


class HistorySource(StrEnum):
    KLINE = "kline"
    TICKER = "ticker"


class HistoryQuality(StrEnum):
    KLINE = "kline"
    MIXED = "mixed"
    LIVE = "live"


class PriceEventKind(StrEnum):
    SNAPSHOT_SEED = "snapshot_seed"
    LIVE = "live"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class DedupState(StrEnum):
    IDLE = "IDLE"
    ACTIVE = "ACTIVE"
