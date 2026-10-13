from __future__ import annotations

from enum import StrEnum


class TradingMode(StrEnum):
    DISABLED = "disabled"
    DEMO = "demo"
    MAINNET = "mainnet"


class TradeAction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    IGNORE = "IGNORE"


class SizingPrice(StrEnum):
    LAST = "lastPrice"
    MARK = "markPrice"


class EntryOrderType(StrEnum):
    MARKET = "MARKET"


class ExistingPositionPolicy(StrEnum):
    SKIP = "skip"


class ExecutionStatus(StrEnum):
    PENDING = "PENDING"
    RESERVED = "RESERVED"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    SKIPPED = "SKIPPED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    PROTECTIVE_FAILED = "PROTECTIVE_FAILED"
    RECONCILING = "RECONCILING"
