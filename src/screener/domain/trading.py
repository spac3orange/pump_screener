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
    # Position lifecycle (after the entry is filled).
    OPEN = "OPEN"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"


# Entry filled => a position may exist on Bybit and must be monitored until size == 0.
TRACKED_STATUSES = frozenset(
    {
        ExecutionStatus.PARTIALLY_FILLED,
        ExecutionStatus.FILLED,
        ExecutionStatus.PROTECTIVE_FAILED,
        ExecutionStatus.OPEN,
        ExecutionStatus.CLOSING,
    }
)

# Set by the position lifecycle monitor; the entry path must never overwrite them.
LIFECYCLE_STATUSES = frozenset(
    {ExecutionStatus.OPEN, ExecutionStatus.CLOSING, ExecutionStatus.CLOSED}
)


def entry_status_change_allowed(current: str, new: str) -> bool:
    """Entry path status update: never undo lifecycle states or untrack a filled entry."""
    if current in LIFECYCLE_STATUSES:
        return False
    return not (current in TRACKED_STATUSES and new not in TRACKED_STATUSES)
