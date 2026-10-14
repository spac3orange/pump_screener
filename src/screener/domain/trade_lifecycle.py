"""Position lifecycle DTOs: Bybit executions/positions mapped to SDK-free values.

Field semantics follow Bybit V5 (execution / position / closed-pnl):
- ``execPnl`` is the gross close PnL (same as transaction-log ``cashFlow``: no fee, no funding);
- ``execFee`` is a positive expense for trades; for ``execType=Funding`` a positive value means
  funding was *paid* (documented as the opposite sign of transaction-log ``funding``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

BUY = "Buy"
SELL = "Sell"

# execType values that move a position (Funding/Settle/... do not).
TRADE_EXEC_TYPES = frozenset({"Trade", "AdlTrade", "BustTrade"})
FUNDING_EXEC_TYPE = "Funding"


class PositionCloseReason(StrEnum):
    TAKE_PROFIT = "TAKE_PROFIT"
    STOP_LOSS = "STOP_LOSS"
    MANUAL = "MANUAL"  # closed from the Bybit web/app position panel (createType=CreateByClosing)
    EXTERNAL_ORDER = "EXTERNAL_ORDER"  # any other closing order not created by this service
    LIQUIDATION = "LIQUIDATION"
    ADL = "ADL"  # auto-deleveraging: forced by the exchange, not a liquidation of this account
    UNKNOWN = "UNKNOWN"


class TradeNotificationType(StrEnum):
    TRADE_CLOSED_TP = "TRADE_CLOSED_TP"
    TRADE_CLOSED_SL = "TRADE_CLOSED_SL"
    TRADE_CLOSED_EXTERNAL = "TRADE_CLOSED_EXTERNAL"
    TRADE_LIQUIDATED = "TRADE_LIQUIDATED"
    TRADE_CLOSED_UNKNOWN = "TRADE_CLOSED_UNKNOWN"


_NOTIFICATION_TYPE = {
    PositionCloseReason.TAKE_PROFIT: TradeNotificationType.TRADE_CLOSED_TP,
    PositionCloseReason.STOP_LOSS: TradeNotificationType.TRADE_CLOSED_SL,
    PositionCloseReason.MANUAL: TradeNotificationType.TRADE_CLOSED_EXTERNAL,
    PositionCloseReason.EXTERNAL_ORDER: TradeNotificationType.TRADE_CLOSED_EXTERNAL,
    PositionCloseReason.LIQUIDATION: TradeNotificationType.TRADE_LIQUIDATED,
    PositionCloseReason.ADL: TradeNotificationType.TRADE_LIQUIDATED,
    PositionCloseReason.UNKNOWN: TradeNotificationType.TRADE_CLOSED_UNKNOWN,
}


@dataclass(frozen=True, slots=True)
class Fill:
    """One Bybit execution (``execution`` topic or ``/v5/execution/list`` row)."""

    exec_id: str
    symbol: str
    order_id: str
    order_link_id: str
    side: str
    exec_type: str
    create_type: str
    stop_order_type: str
    price: Decimal
    qty: Decimal
    closed_size: Decimal
    fee: Decimal
    pnl: Decimal | None
    time_ms: int
    seq: int = 0

    @property
    def is_trade(self) -> bool:
        return self.exec_type in TRADE_EXEC_TYPES

    @property
    def is_funding(self) -> bool:
        return self.exec_type == FUNDING_EXEC_TYPE


@dataclass(frozen=True, slots=True)
class PositionState:
    """One-way position snapshot (``position`` topic or ``/v5/position/list``)."""

    symbol: str
    size: Decimal
    side: str | None
    status: str = ""
    seq: int = 0
    updated_ms: int = 0


@dataclass(frozen=True, slots=True)
class ProtectiveOrder:
    """System TP/SL conditional order created by ``set_trading_stop`` (``order`` topic)."""

    order_id: str
    symbol: str
    stop_order_type: str
    status: str
    cancel_type: str = ""


@dataclass(frozen=True, slots=True)
class ClosedPnlRecord:
    """``/v5/position/closed-pnl`` row: Bybit's own per-close-order summary."""

    order_id: str
    symbol: str
    closed_pnl: Decimal
    avg_exit_price: Decimal | None
    closed_size: Decimal
    updated_ms: int
    open_fee: Decimal | None = None
    close_fee: Decimal | None = None


@dataclass(slots=True)
class OpenTrade:
    """Local journal row (``trading_executions``) whose position is being monitored."""

    trade_id: UUID
    signal_id: UUID
    symbol: str
    side: str
    qty: Decimal
    entry_price: Decimal | None
    entry_order_id: str | None
    order_link_id: str
    opened_at: datetime
    status: str
    mode: str
    take_profit: Decimal | None = None
    stop_loss: Decimal | None = None
    tp_order_id: str | None = None
    sl_order_id: str | None = None

    @property
    def direction(self) -> str:
        return "LONG" if self.side == BUY else "SHORT"

    @property
    def opened_ms(self) -> int:
        return int(self.opened_at.timestamp() * 1000)

    def is_closing_fill(self, fill: Fill) -> bool:
        return (
            fill.symbol == self.symbol
            and fill.is_trade
            and fill.side != self.side
            and fill.closed_size > 0
            and fill.time_ms >= self.opened_ms - 1000
        )


@dataclass(frozen=True, slots=True)
class CloseClassification:
    reason: PositionCloseReason
    detail: str
    close_order_id: str | None
    close_execution_id: str | None


@dataclass(frozen=True, slots=True)
class TradeClosedEvent:
    trade_id: UUID
    signal_id: UUID
    symbol: str
    side: str
    mode: str
    close_reason: PositionCloseReason
    reason_detail: str
    entry_price: Decimal | None
    exit_price: Decimal | None
    qty: Decimal
    closed_qty: Decimal
    opened_at: datetime
    closed_at: datetime
    realized_pnl: Decimal | None
    fees: Decimal | None
    funding: Decimal | None
    net_pnl: Decimal | None
    bybit_closed_pnl: Decimal | None
    balance_after: Decimal | None
    close_order_id: str | None
    close_execution_id: str | None
    recovered: bool = False

    @property
    def direction(self) -> str:
        return "LONG" if self.side == BUY else "SHORT"

    @property
    def duration(self) -> timedelta:
        return max(self.closed_at - self.opened_at, timedelta(0))

    @property
    def price_pnl_pct(self) -> Decimal | None:
        """Price move in the trade's favour, % of entry (not ROI on margin)."""
        if self.entry_price is None or self.exit_price is None or self.entry_price <= 0:
            return None
        move = self.exit_price - self.entry_price
        if self.side == SELL:
            move = -move
        return move / self.entry_price * 100

    @property
    def notification_type(self) -> TradeNotificationType:
        return _NOTIFICATION_TYPE[self.close_reason]
