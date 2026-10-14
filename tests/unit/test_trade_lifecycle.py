"""PositionLifecycleService: OPEN → size 0 → classify → persist → exactly one notification."""

from __future__ import annotations

import asyncio
import dataclasses
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from screener.application.metrics import metrics
from screener.application.trading.lifecycle import LifecycleConfig, PositionLifecycleService
from screener.domain.trade_lifecycle import (
    ClosedPnlRecord,
    Fill,
    OpenTrade,
    PositionCloseReason,
    PositionState,
    ProtectiveOrder,
    TradeClosedEvent,
)

D = Decimal
SYMBOL = "EPICUSDT"


def _now_ms() -> int:
    return int(time.time() * 1000)


def make_trade(*, side: str = "Buy", qty: str = "100", opened_ago_sec: float = 600) -> OpenTrade:
    return OpenTrade(
        trade_id=uuid4(),
        signal_id=uuid4(),
        symbol=SYMBOL,
        side=side,
        qty=D(qty),
        entry_price=D("0.3102"),
        entry_order_id="entry-1",
        order_link_id="link-1",
        opened_at=datetime.now(UTC) - timedelta(seconds=opened_ago_sec),
        status="FILLED",
        mode="demo",
        take_profit=D("0.3291"),
        stop_loss=D("0.3009"),
    )


_seq = 0


def fill(
    *,
    exec_id: str | None = None,
    order_id: str = "close-1",
    side: str = "Sell",
    price: str = "0.3291",
    qty: str = "100",
    closed: str | None = None,
    fee: str = "0.0181",
    pnl: str | None = "1.89",
    stop_order_type: str = "",
    create_type: str = "",
    exec_type: str = "Trade",
    order_link_id: str = "",
    time_ms: int | None = None,
) -> Fill:
    global _seq
    _seq += 1
    return Fill(
        exec_id=exec_id or f"x{_seq}",
        symbol=SYMBOL,
        order_id=order_id,
        order_link_id=order_link_id,
        side=side,
        exec_type=exec_type,
        create_type=create_type,
        stop_order_type=stop_order_type,
        price=D(price),
        qty=D(qty),
        closed_size=D(closed if closed is not None else qty),
        fee=D(fee),
        pnl=D(pnl) if pnl is not None else None,
        time_ms=time_ms or _now_ms(),
        seq=_seq,
    )


def entry_fill(trade: OpenTrade) -> Fill:
    return fill(
        exec_id="entry-x",
        order_id="entry-1",
        side=trade.side,
        price="0.3102",
        closed="0",
        fee="0.0171",
        pnl="0",
        create_type="CreateByUser",
        order_link_id="link-1",
        time_ms=trade.opened_ms,
    )


def tp_fill(**kw) -> Fill:
    return fill(stop_order_type="TakeProfit", create_type="CreateByTakeProfit", **kw)


def sl_fill(**kw) -> Fill:
    kw.setdefault("price", "0.3009")
    kw.setdefault("pnl", "-0.93")
    return fill(stop_order_type="StopLoss", create_type="CreateByStopLoss", **kw)


class FakeGateway:
    def __init__(self) -> None:
        self.positions: dict[str, PositionState] = {}
        self.fills: list[Fill] = []
        self.closed_pnl: list[ClosedPnlRecord] = []
        self.protective: list[ProtectiveOrder] = []
        self.equity: Decimal | None = D("10.73")
        self.fail_fills = False

    def open(self, size: str = "100", side: str = "Buy") -> None:
        self.positions[SYMBOL] = PositionState(symbol=SYMBOL, size=D(size), side=side)

    def close(self) -> None:
        self.positions.pop(SYMBOL, None)

    async def get_open_positions(self) -> dict[str, PositionState]:
        return dict(self.positions)

    async def get_position(self, symbol: str) -> PositionState:
        return self.positions.get(symbol) or PositionState(symbol=symbol, size=D(0), side=None)

    async def get_fills(self, symbol: str, start_ms: int, end_ms: int) -> list[Fill]:
        if self.fail_fills:
            raise RuntimeError("rest down")
        return [f for f in self.fills if start_ms <= f.time_ms <= end_ms]

    async def get_order_fills(self, symbol: str, order_id: str) -> list[Fill]:
        return [f for f in self.fills if f.order_id == order_id]

    async def get_closed_pnl(self, symbol: str, start_ms: int, end_ms: int):
        return list(self.closed_pnl)

    async def get_protective_orders(self, symbol: str) -> list[ProtectiveOrder]:
        return list(self.protective)

    async def get_equity(self) -> Decimal:
        if self.equity is None:
            raise RuntimeError("balance down")
        return self.equity


@dataclasses.dataclass
class _Row:
    trade: OpenTrade
    remaining: Decimal | None = None
    closed: TradeClosedEvent | None = None
    notified_at: datetime | None = None


class FakeStore:
    """Same atomic semantics as TradingRepository (status guard + notification claim)."""

    def __init__(self, *trades: OpenTrade) -> None:
        self.rows: dict[UUID, _Row] = {t.trade_id: _Row(dataclasses.replace(t)) for t in trades}

    def row(self, trade: OpenTrade) -> _Row:
        return self.rows[trade.trade_id]

    def _tracked(self, mode: str) -> list[_Row]:
        return [
            r for r in self.rows.values() if r.trade.mode == mode and r.trade.status != "CLOSED"
        ]

    async def list_tracked_trades(self, mode: str) -> list[OpenTrade]:
        return [dataclasses.replace(r.trade) for r in self._tracked(mode)]

    async def find_tracked_trade(self, symbol: str, mode: str) -> OpenTrade | None:
        rows = [r for r in self._tracked(mode) if r.trade.symbol == symbol]
        return dataclasses.replace(rows[-1].trade) if rows else None

    async def mark_open(self, trade_id, *, remaining_qty, tp_order_id, sl_order_id) -> None:
        row = self.rows[trade_id]
        if row.trade.status != "CLOSED":
            row.trade.status = "OPEN"
            row.remaining = remaining_qty
            row.trade.tp_order_id = tp_order_id
            row.trade.sl_order_id = sl_order_id

    async def update_remaining(self, trade_id, remaining_qty) -> None:
        row = self.rows[trade_id]
        if row.trade.status != "CLOSED":
            row.remaining = remaining_qty

    async def mark_closing(self, trade_id) -> None:
        row = self.rows[trade_id]
        if row.trade.status != "CLOSED":
            row.trade.status = "CLOSING"

    async def finalize_close(self, event: TradeClosedEvent) -> bool:
        row = self.rows[event.trade_id]
        if row.trade.status == "CLOSED":
            return False
        row.trade.status = "CLOSED"
        row.closed = event
        row.remaining = D(0)
        return True

    async def claim_close_notification(self, trade_id) -> bool:
        row = self.rows[trade_id]
        if row.trade.status != "CLOSED" or row.notified_at is not None:
            return False
        row.notified_at = datetime.now(UTC)
        return True

    async def release_close_notification(self, trade_id) -> None:
        self.rows[trade_id].notified_at = None

    async def list_unnotified_closed(self, mode: str, since: datetime):
        return [
            r.closed
            for r in self.rows.values()
            if r.closed is not None and r.notified_at is None and r.closed.closed_at >= since
        ]

    async def last_closed(self, mode: str):
        closed = [r.closed for r in self.rows.values() if r.closed is not None]
        return closed[-1] if closed else None


class Notifier:
    def __init__(self, *, fail_times: int = 0) -> None:
        self.events: list[TradeClosedEvent] = []
        self.fail_times = fail_times

    async def __call__(self, event: TradeClosedEvent) -> bool:
        if self.fail_times > 0:
            self.fail_times -= 1
            return False
        self.events.append(event)
        return True


WINDOW = 0.1


def make_service(gw: FakeGateway, store: FakeStore, notifier: Notifier, **cfg):
    config = LifecycleConfig(
        mode="demo",
        correlation_window_sec=cfg.pop("window", WINDOW),
        pnl_wait_sec=0.0,
        reconcile_interval_sec=3600,
        **cfg,
    )
    alerts: list[str] = []

    async def alert(text: str) -> None:
        alerts.append(text)

    service = PositionLifecycleService(gw, store, config, notify=notifier, alert=alert)
    service.alerts = alerts  # type: ignore[attr-defined]
    return service


async def settle(seconds: float = WINDOW * 3) -> None:
    await asyncio.sleep(seconds)


@pytest.fixture
async def env():
    trade = make_trade()
    gw = FakeGateway()
    gw.open()
    gw.fills.append(entry_fill(trade))
    gw.protective = [
        ProtectiveOrder("tp-1", SYMBOL, "TakeProfit", "Untriggered"),
        ProtectiveOrder("sl-1", SYMBOL, "StopLoss", "Untriggered"),
    ]
    store = FakeStore(trade)
    notifier = Notifier()
    service = make_service(gw, store, notifier)
    await service.start()
    yield trade, gw, store, notifier, service
    await service.stop()


def zero() -> PositionState:
    return PositionState(symbol=SYMBOL, size=D(0), side=None)


async def test_startup_marks_open_and_stores_protective_ids(env) -> None:
    trade, _gw, store, notifier, _ = env
    row = store.row(trade)
    assert row.trade.status == "OPEN"
    assert row.remaining == D("100")
    assert (row.trade.tp_order_id, row.trade.sl_order_id) == ("tp-1", "sl-1")
    assert notifier.events == []


async def test_protective_ids_from_ws_are_persisted() -> None:
    trade = make_trade()
    gw = FakeGateway()
    gw.open()  # REST shows no TP/SL yet
    store = FakeStore(trade)
    service = make_service(gw, store, Notifier())
    await service.start()
    try:
        assert store.row(trade).trade.tp_order_id is None
        await service.on_protective_orders(
            [
                ProtectiveOrder("tp-2", SYMBOL, "TakeProfit", "Untriggered"),
                ProtectiveOrder("sl-2", SYMBOL, "StopLoss", "Untriggered"),
            ]
        )
    finally:
        await service.stop()
    row = store.row(trade)
    assert (row.trade.tp_order_id, row.trade.sl_order_id) == ("tp-2", "sl-2")


async def test_take_profit_exactly_one_notification(env) -> None:
    trade, gw, store, notifier, service = env
    sent_before = metrics.close_notifications_sent_total
    close = tp_fill(order_id="tp-1")
    gw.fills.append(close)
    gw.close()
    await service.on_protective_orders(
        [ProtectiveOrder("tp-1", SYMBOL, "TakeProfit", "Triggered")]
    )
    await service.on_fills([close])
    await service.on_positions([zero()])
    # SL leg cancelled by the exchange + duplicate / reconnect replays.
    await service.on_protective_orders(
        [ProtectiveOrder("sl-1", SYMBOL, "StopLoss", "Deactivated", "CancelByTpSlTsClear")]
    )
    await service.on_fills([close])
    await service.on_positions([zero(), zero()])
    await settle()
    await service.reconcile_all()

    assert len(notifier.events) == 1
    event = notifier.events[0]
    assert event.close_reason is PositionCloseReason.TAKE_PROFIT
    assert event.exit_price == D("0.3291")
    assert event.realized_pnl == D("1.89")
    assert event.fees == D("0.0171") + D("0.0181")
    assert event.funding == D(0)
    assert event.net_pnl == D("1.89") - D("0.0352")
    assert event.balance_after == D("10.73")
    assert event.close_order_id == "tp-1"
    assert store.row(trade).trade.status == "CLOSED"
    assert metrics.close_notifications_sent_total == sent_before + 1


async def test_stop_loss_with_tp_cancelled(env) -> None:
    _trade, gw, _store, notifier, service = env
    close = sl_fill(order_id="sl-1")
    gw.fills.append(close)
    gw.close()
    await service.on_fills([close])
    await service.on_protective_orders(
        [ProtectiveOrder("tp-1", SYMBOL, "TakeProfit", "Deactivated", "CancelByTpSlTsClear")]
    )
    await service.on_positions([zero()])
    await settle()

    assert [e.close_reason for e in notifier.events] == [PositionCloseReason.STOP_LOSS]
    assert notifier.events[0].price_pnl_pct < 0


async def test_cancelled_tp_sl_while_position_open_is_not_a_close(env) -> None:
    trade, _gw, store, notifier, service = env
    await service.on_protective_orders(
        [
            ProtectiveOrder("tp-1", SYMBOL, "TakeProfit", "Cancelled", "CancelByUser"),
            ProtectiveOrder("sl-1", SYMBOL, "StopLoss", "Cancelled", "CancelByUser"),
        ]
    )
    await service.on_positions([PositionState(SYMBOL, D("100"), "Buy")])
    await settle()
    await service.reconcile_all()
    assert notifier.events == []
    assert store.row(trade).trade.status == "OPEN"


async def test_partial_close_updates_remaining_without_notification(env) -> None:
    trade, gw, store, notifier, service = env
    part = tp_fill(order_id="ptp-1", qty="40", pnl="0.75")
    part = dataclasses.replace(part, stop_order_type="PartialTakeProfit")
    gw.fills.append(part)
    gw.open("60")
    await service.on_fills([part])
    await service.on_positions([PositionState(SYMBOL, D("60"), "Buy")])
    await settle()
    await service.reconcile_all()
    assert notifier.events == []
    assert store.row(trade).remaining == D("60")
    assert store.row(trade).trade.status == "OPEN"

    rest = sl_fill(order_id="sl-1", qty="60", pnl="-0.56")
    gw.fills.append(rest)
    gw.close()
    await service.on_fills([rest])
    await service.on_positions([zero()])
    await settle()
    assert len(notifier.events) == 1
    event = notifier.events[0]
    assert event.close_reason is PositionCloseReason.STOP_LOSS  # the order that zeroed it
    assert event.closed_qty == D("100")
    assert event.realized_pnl == D("0.75") + D("-0.56")


async def test_manual_close_from_bybit_ui(env) -> None:
    _trade, gw, _store, notifier, service = env
    close = fill(create_type="CreateByClosing", price="0.3200", pnl="0.98")
    gw.fills.append(close)
    gw.close()
    await service.on_fills([close])
    await service.on_positions([zero()])
    await settle()
    assert [e.close_reason for e in notifier.events] == [PositionCloseReason.MANUAL]


async def test_external_reduce_only_order(env) -> None:
    _trade, gw, _store, notifier, service = env
    close = fill(create_type="CreateByUser", price="0.3200", pnl="0.98")
    gw.fills.append(close)
    gw.close()
    await service.on_fills([close])
    await service.on_positions([zero()])
    await settle()
    assert [e.close_reason for e in notifier.events] == [PositionCloseReason.EXTERNAL_ORDER]


async def test_exit_near_tp_price_without_tp_evidence_is_not_take_profit(env) -> None:
    _trade, gw, _store, notifier, service = env
    close = fill(price="0.3291", create_type="")  # exactly the TP price, no mechanism fields
    gw.fills.append(close)
    gw.close()
    await service.on_fills([close])
    await service.on_positions([zero()])
    await settle()
    assert [e.close_reason for e in notifier.events] == [PositionCloseReason.UNKNOWN]


async def test_unknown_when_no_execution_data(env) -> None:
    _trade, gw, _store, notifier, service = env
    gw.close()
    await service.on_positions([zero()])
    await settle()
    assert len(notifier.events) == 1
    event = notifier.events[0]
    assert event.close_reason is PositionCloseReason.UNKNOWN
    assert event.realized_pnl is None and event.net_pnl is None and event.exit_price is None


async def test_position_zero_before_execution_waits_for_correlation(env) -> None:
    _trade, gw, _store, notifier, service = env
    close = tp_fill(order_id="tp-1")
    gw.close()
    await service.on_positions([zero()])
    await asyncio.sleep(WINDOW / 4)
    assert notifier.events == []
    await service.on_fills([close])  # complete evidence: finalize right away
    await asyncio.sleep(WINDOW / 4)
    assert [e.close_reason for e in notifier.events] == [PositionCloseReason.TAKE_PROFIT]


async def test_execution_before_position_zero_does_not_finalize(env) -> None:
    _trade, gw, _store, notifier, service = env
    close = tp_fill(order_id="tp-1")
    await service.on_fills([close])
    await settle()
    assert notifier.events == []  # position still size > 0
    gw.fills.append(close)
    gw.close()
    await service.on_positions([zero()])
    await settle()
    assert len(notifier.events) == 1


async def test_ws_zero_but_rest_still_open_aborts(env) -> None:
    trade, _gw, store, notifier, service = env
    await service.on_positions([zero()])  # stale/garbled WS; REST still shows the position
    await settle()
    assert notifier.events == []
    assert store.row(trade).trade.status == "OPEN"


async def test_liquidation(env) -> None:
    _trade, gw, _store, notifier, service = env
    close = fill(exec_type="BustTrade", create_type="CreateByLiq", price="0.2100", pnl="-3.3")
    gw.fills.append(close)
    gw.close()
    await service.on_fills([close])
    await service.on_positions([zero()])
    await settle()
    assert [e.close_reason for e in notifier.events] == [PositionCloseReason.LIQUIDATION]


async def test_balance_unavailable_does_not_block(env) -> None:
    _trade, gw, _store, notifier, service = env
    gw.equity = None
    close = tp_fill(order_id="tp-1")
    gw.fills.append(close)
    gw.close()
    await service.on_fills([close])
    await service.on_positions([zero()])
    await settle()
    assert notifier.events[0].balance_after is None


async def test_rest_failure_leaves_funding_unknown(env) -> None:
    _trade, gw, _store, notifier, service = env
    close = tp_fill(order_id="tp-1")
    gw.fail_fills = True
    gw.close()
    await service.on_fills([close])
    await service.on_positions([zero()])
    await settle()
    event = notifier.events[0]
    assert event.close_reason is PositionCloseReason.TAKE_PROFIT
    assert event.funding is None and event.net_pnl is None
    assert event.realized_pnl == D("1.89")


async def test_funding_is_subtracted_from_net(env) -> None:
    trade, gw, _store, notifier, service = env
    gw.fills.append(
        fill(exec_type="Funding", fee="0.004", pnl=None, closed="0", time_ms=trade.opened_ms + 5)
    )
    close = tp_fill(order_id="tp-1")
    gw.fills.append(close)
    gw.close()
    await service.on_fills([close])
    await service.on_positions([zero()])
    await settle()
    event = notifier.events[0]
    assert event.funding == D("0.004")
    assert event.net_pnl == D("1.89") - D("0.0352") - D("0.004")


async def test_bybit_closed_pnl_is_cross_check_only(env) -> None:
    _trade, gw, _store, notifier, service = env
    close = tp_fill(order_id="tp-1")
    gw.fills.append(close)
    gw.closed_pnl = [ClosedPnlRecord("tp-1", SYMBOL, D("1.85"), D("0.3291"), D("100"), 0)]
    gw.close()
    await service.on_fills([close])
    await service.on_positions([zero()])
    await settle()
    event = notifier.events[0]
    assert event.bybit_closed_pnl == D("1.85")
    assert event.net_pnl == D("1.89") - D("0.0352")


async def test_notification_failure_is_retried_once_delivered() -> None:
    trade = make_trade()
    gw = FakeGateway()
    gw.open()
    store = FakeStore(trade)
    notifier = Notifier(fail_times=1)
    service = make_service(gw, store, notifier)
    await service.start()
    try:
        close = tp_fill()
        gw.fills.append(close)
        gw.close()
        await service.on_fills([close])
        await service.on_positions([zero()])
        await settle()
        assert notifier.events == []
        assert store.row(trade).notified_at is None
        await service.reconcile_all()
        await service.reconcile_all()
        assert len(notifier.events) == 1
    finally:
        await service.stop()


async def test_restart_recovery_notifies_missed_close_once() -> None:
    trade = make_trade(opened_ago_sec=1800)
    gw = FakeGateway()  # position already gone on Bybit
    gw.fills += [entry_fill(trade), tp_fill(time_ms=trade.opened_ms + 60_000)]
    store = FakeStore(trade)
    notifier = Notifier()

    service = make_service(gw, store, notifier)
    await service.start()
    await service.stop()
    assert len(notifier.events) == 1
    event = notifier.events[0]
    assert event.close_reason is PositionCloseReason.TAKE_PROFIT
    assert event.recovered is True
    assert event.balance_after is None  # current equity is not "balance after close"

    again = make_service(gw, store, notifier)
    await again.start()
    await again.on_positions([zero()])
    await settle()
    await again.stop()
    assert len(notifier.events) == 1


async def test_restart_recovery_of_old_close_is_recorded_silently() -> None:
    trade = make_trade(opened_ago_sec=3 * 86400)
    gw = FakeGateway()
    gw.fills.append(tp_fill(time_ms=trade.opened_ms + 60_000))
    store = FakeStore(trade)
    notifier = Notifier()
    service = make_service(gw, store, notifier)
    await service.start()
    await service.stop()
    assert notifier.events == []
    assert store.row(trade).trade.status == "CLOSED"
    assert store.row(trade).notified_at is not None


async def test_notify_disabled_records_without_sending() -> None:
    trade = make_trade()
    gw = FakeGateway()
    gw.open()
    store = FakeStore(trade)
    notifier = Notifier()
    service = make_service(gw, store, notifier, notify_closes=False)
    await service.start()
    close = tp_fill()
    gw.fills.append(close)
    gw.close()
    await service.on_fills([close])
    await service.on_positions([zero()])
    await settle()
    await service.stop()
    assert notifier.events == []
    assert store.row(trade).closed is not None


async def test_ws_reconnect_triggers_reconcile_of_missed_close(env) -> None:
    _trade, gw, _store, notifier, service = env
    service.on_stream_status(True)
    service.on_stream_status(False)
    gw.fills.append(tp_fill(order_id="tp-1"))
    gw.close()  # closed while disconnected: no WS events at all
    service.on_stream_status(True)
    await settle()
    assert [e.close_reason for e in notifier.events] == [PositionCloseReason.TAKE_PROFIT]
    assert metrics.private_ws_connected is True


async def test_short_trade_price_pnl_sign() -> None:
    trade = make_trade(side="Sell")
    gw = FakeGateway()
    gw.open(side="Sell")
    store = FakeStore(trade)
    notifier = Notifier()
    service = make_service(gw, store, notifier)
    await service.start()
    close = sl_fill(side="Buy", price="0.3009", pnl="0.93")  # short: exit below entry = profit
    gw.fills.append(close)
    gw.close()
    await service.on_fills([close])
    await service.on_positions([zero()])
    await settle()
    await service.stop()
    event = notifier.events[0]
    assert event.direction == "SHORT"
    assert event.price_pnl_pct == (D("0.3102") - D("0.3009")) / D("0.3102") * 100


async def test_lifecycle_has_no_trading_on_off_dependency(env) -> None:
    """Trading OFF only blocks new entries; the monitor is a separate service."""
    _trade, gw, _store, notifier, service = env
    metrics.trading_enabled = False
    close = tp_fill(order_id="tp-1")
    gw.fills.append(close)
    gw.close()
    await service.on_fills([close])
    await service.on_positions([zero()])
    await settle()
    assert len(notifier.events) == 1


async def test_untracked_symbol_events_are_ignored(env) -> None:
    _trade, _gw, _store, notifier, service = env
    other = dataclasses.replace(tp_fill(), symbol="BTCUSDT")
    await service.on_fills([other])
    await service.on_positions([PositionState("BTCUSDT", D(0), None)])
    await settle()
    assert notifier.events == []


async def test_entry_fill_before_trade_is_persisted_is_kept() -> None:
    """WS entry execution precedes the FILLED row; REST history is still empty."""
    trade = make_trade()
    gw = FakeGateway()  # REST knows nothing (lag)
    store = FakeStore()
    notifier = Notifier()
    service = make_service(gw, store, notifier)
    await service.start()
    try:
        await service.on_fills([entry_fill(trade)])  # not tracked yet → buffered
        store.rows[trade.trade_id] = _Row(dataclasses.replace(trade))
        service.notice_trade_opened(SYMBOL)
        gw.open()
        await service.on_positions([PositionState(SYMBOL, D("100"), "Buy")])
        close = tp_fill()
        await service.on_fills([close])
        gw.close()
        await service.on_positions([zero()])
        await settle()
    finally:
        await service.stop()
    event = notifier.events[0]
    assert event.fees == D("0.0171") + D("0.0181")
    assert event.net_pnl == D("1.89") - D("0.0352")


async def test_reconcile_waits_for_lagging_rest_history() -> None:
    trade = make_trade()
    gw = FakeGateway()
    gw.open()
    store = FakeStore(trade)
    notifier = Notifier()
    service = make_service(gw, store, notifier, window=0.05)
    service._cfg = dataclasses.replace(service._cfg, pnl_wait_sec=1.0)  # noqa: SLF001
    await service.start()
    try:
        gw.close()  # closed while WS was down; REST executions appear 0.3 s later
        task = asyncio.create_task(service.reconcile_all())
        await asyncio.sleep(0.3)
        gw.fills += [entry_fill(trade), tp_fill()]
        await task
    finally:
        await service.stop()
    assert [e.close_reason for e in notifier.events] == [PositionCloseReason.TAKE_PROFIT]


async def test_bybit_closed_pnl_fallback_requires_every_closing_order() -> None:
    trade = make_trade()
    gw = FakeGateway()
    gw.open()
    store = FakeStore(trade)
    notifier = Notifier()
    service = make_service(gw, store, notifier)
    await service.start()
    try:
        # REST-only data (no execPnl) from two closing orders.
        a = tp_fill(order_id="a", qty="40", pnl=None)
        b = tp_fill(order_id="b", qty="60", pnl=None)
        gw.fills += [a, b]
        gw.closed_pnl = [
            ClosedPnlRecord("a", SYMBOL, D("0.70"), D("0.33"), D("40"), 1, D("0.007"), D("0.007")),
        ]
        gw.close()
        await service.on_positions([zero()])
        await settle()
    finally:
        await service.stop()
    event = notifier.events[0]
    assert event.realized_pnl is None and event.net_pnl is None
    assert event.bybit_closed_pnl is None  # order "b" has no record: no partial sum
    assert event.fees is None


async def test_bybit_closed_pnl_aggregates_all_closing_orders() -> None:
    trade = make_trade()
    gw = FakeGateway()
    gw.open()
    store = FakeStore(trade)
    notifier = Notifier()
    service = make_service(gw, store, notifier)
    await service.start()
    try:
        gw.fills += [tp_fill(order_id="a", qty="40", pnl=None), tp_fill(order_id="b", qty="60")]
        gw.closed_pnl = [
            ClosedPnlRecord("a", SYMBOL, D("0.70"), D("0.33"), D("40"), 1, D("0.007"), D("0.007")),
            ClosedPnlRecord("b", SYMBOL, D("1.05"), D("0.32"), D("60"), 2, D("0.01"), D("0.011")),
        ]
        gw.close()
        await service.on_positions([zero()])
        await settle()
    finally:
        await service.stop()
    event = notifier.events[0]
    assert event.bybit_closed_pnl == D("1.75")
    assert event.fees == D("0.035")  # from Bybit openFee + closeFee (no entry execution seen)


async def test_stream_down_alert_after_threshold(env) -> None:
    _trade, _gw, _store, _notifier, service = env
    service.on_stream_status(False)
    service._stream_down_since = time.monotonic() - 3600  # noqa: SLF001
    await service._check_stream()  # noqa: SLF001
    await service._check_stream()  # noqa: SLF001
    assert len(service.alerts) == 1
    assert "TP/SL" in service.alerts[0]
