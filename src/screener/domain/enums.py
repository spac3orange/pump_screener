from __future__ import annotations

from enum import StrEnum


class SignalType(StrEnum):
    PUMP = "PUMP"
    DUMP = "DUMP"
    VOLUME_SPIKE = "VOLUME_SPIKE"
    COMPOSITE = "COMPOSITE"


class SideHint(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


class PriceSource(StrEnum):
    LAST = "lastPrice"


class HistorySource(StrEnum):
    KLINE = "kline"
    TICKER = "ticker"


class HistoryQuality(StrEnum):
    KLINE = "kline"
    MIXED = "mixed"
    LIVE = "live"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class DedupState(StrEnum):
    IDLE = "IDLE"
    ACTIVE = "ACTIVE"
