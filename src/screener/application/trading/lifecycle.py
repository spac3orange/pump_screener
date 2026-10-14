"""OPEN position → monitor → size == 0 → classify → persist → exactly one notification.

Sources: Bybit private WS (execution / position / order) for realtime, REST for startup,
reconnect and periodic fallback reconciliation. Entry trading (TradingService) is untouched;
this service runs whenever the trading module is configured, even with Trading OFF.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from screener.application.metrics import metrics
from screener.application.supervisor import cancel_tasks, supervise
from screener.application.trading.close_classifier import classify_close
from screener.domain.trade_lifecycle import (
    ClosedPnlRecord,
    Fill,
    OpenTrade,
    PositionState,
    ProtectiveOrder,
    TradeClosedEvent,
)
from screener.domain.trading import ExecutionStatus
from screener.logging import log

logger = logging.getLogger(__name__)

_TS_LOOKBACK_MS = 60_000
_NEGATIVE_LOOKUP_TTL_SEC = 2.0
_BALANCE_TIMEOUT_SEC = 5.0
_RESEND_WINDOW = timedelta(hours=24)
_RECENT_FILLS_PER_SYMBOL = 32
_RECENT_SYMBOLS_MAX = 64


class LifecycleGateway(Protocol):
    async def get_open_positions(self) -> dict[str, PositionState]: ...

    async def get_position(self, symbol: str) -> PositionState: ...

    async def get_fills(self, symbol: str, start_ms: int, end_ms: int) -> list[Fill]: ...

    async def get_order_fills(self, symbol: str, order_id: str) -> list[Fill]: ...

    async def get_closed_pnl(
        self, symbol: str, start_ms: int, end_ms: int
    ) -> list[ClosedPnlRecord]: ...

    async def get_protective_orders(self, symbol: str) -> list[ProtectiveOrder]: ...

    async def get_equity(self) -> Decimal: ...


class LifecycleStore(Protocol):
    async def list_tracked_trades(self, mode: str) -> list[OpenTrade]: ...

    async def find_tracked_trade(self, symbol: str, mode: str) -> OpenTrade | None: ...

    async def mark_open(
        self,
        trade_id: UUID,
        *,
        remaining_qty: Decimal,
        tp_order_id: str | None,
        sl_order_id: str | None,
    ) -> None: ...

    async def update_remaining(self, trade_id: UUID, remaining_qty: Decimal) -> None: ...

    async def mark_closing(self, trade_id: UUID) -> None: ...

    async def finalize_close(self, event: TradeClosedEvent) -> bool: ...

    async def claim_close_notification(self, trade_id: UUID) -> bool: ...

    async def release_close_notification(self, trade_id: UUID) -> None: ...

    async def list_unnotified_closed(
        self, mode: str, since: datetime
    ) -> list[TradeClosedEvent]: ...

    async def last_closed(self, mode: str) -> TradeClosedEvent | None: ...


CloseNotifier = Callable[[TradeClosedEvent], Awaitable[bool]]
AlertFunc = Callable[[str], Awaitable[None]]


@dataclass(slots=True, frozen=True)
class LifecycleConfig:
    mode: str
    notify_closes: bool = True
    correlation_window_sec: float = 3.0
    pnl_wait_sec: float = 3.0
    reconcile_interval_sec: float = 45.0
    recovery_notify_max_age_sec: float = 6 * 3600
    ws_alert_after_sec: float = 120.0


@dataclass(slots=True)
class LifecycleStatus:
    running: bool
    stream_connected: bool
    tracked: int
    last_closed: TradeClosedEvent | None
    last_reconcile_at: datetime | None
    last_error: str


@dataclass(slots=True)
class _Tracked:
    trade: OpenTrade
    fills: dict[str, Fill] = field(default_factory=dict)
    remaining: Decimal = Decimal("0")
    zero_seen: bool = False
    timer: asyncio.TimerHandle | None = None

    def closing_fills(self) -> list[Fill]:
        return [f for f in self.fills.values() if self.trade.is_closing_fill(f)]

    def closed_qty(self) -> Decimal:
        return sum((f.closed_size for f in self.closing_fills()), Decimal("0"))

    def evidence_complete(self) -> bool:
        return self.closed_qty() >= self.trade.qty > 0


def _sum(values: Iterable[Decimal]) -> Decimal:
    return sum(values, Decimal("0"))


def _aggregate_closed_pnl(symbol: str, rows: list[ClosedPnlRecord]) -> ClosedPnlRecord:
    if len(rows) == 1:
        return rows[0]
    size = _sum(r.closed_size for r in rows)
    priced = [r for r in rows if r.avg_exit_price is not None]
    exit_price = (
        _sum(r.avg_exit_price * r.closed_size for r in priced) / size  # type: ignore[operator]
        if size > 0 and len(priced) == len(rows)
        else None
    )
    open_fees = [r.open_fee for r in rows]
    close_fees = [r.close_fee for r in rows]
    return ClosedPnlRecord(
        order_id=rows[-1].order_id,
        symbol=symbol,
        closed_pnl=_sum(r.closed_pnl for r in rows),
        avg_exit_price=exit_price,
        closed_size=size,
        updated_ms=max(r.updated_ms for r in rows),
        open_fee=None if None in open_fees else _sum(open_fees),  # type: ignore[arg-type]
        close_fee=None if None in close_fees else _sum(close_fees),  # type: ignore[arg-type]
    )


class PositionLifecycleService:
    def __init__(
        self,
        gateway: LifecycleGateway,
        store: LifecycleStore,
        config: LifecycleConfig,
        *,
        notify: CloseNotifier | None = None,
        alert: AlertFunc | None = None,
    ) -> None:
        self._gw = gateway
        self._store = store
        self._cfg = config
        self._notify = notify
        self._alert = alert
        self._tracked: dict[str, _Tracked] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._misses: dict[str, float] = {}
        self._recent: dict[str, deque[Fill]] = {}
        self._tasks: list[asyncio.Task] = []
        self._background: set[asyncio.Task] = set()
        self._running = False
        self._stream_connected = False
        self._stream_down_since: float | None = time.monotonic()
        self._stream_alerted = False
        self._last_closed: TradeClosedEvent | None = None
        self._last_reconcile_at: datetime | None = None
        self._last_error = ""

    # --- wiring -------------------------------------------------------------------

    def set_notify(self, notify: CloseNotifier | None, alert: AlertFunc | None = None) -> None:
        self._notify = notify
        if alert is not None:
            self._alert = alert

    def status(self) -> LifecycleStatus:
        return LifecycleStatus(
            running=self._running,
            stream_connected=self._stream_connected,
            tracked=len(self._tracked),
            last_closed=self._last_closed,
            last_reconcile_at=self._last_reconcile_at,
            last_error=self._last_error,
        )

    async def start(self) -> None:
        self._running = True
        try:
            self._last_closed = await self._store.last_closed(self._cfg.mode)
        except Exception:
            logger.warning("Could not load last closed trade", exc_info=True)
        await self.reconcile_all(recovered=True)
        self._tasks.append(supervise("trade-lifecycle-reconcile", self._reconcile_loop))

    async def stop(self) -> None:
        self._running = False
        for tracked in self._tracked.values():
            if tracked.timer is not None:
                tracked.timer.cancel()
        await cancel_tasks(self._tasks)
        await cancel_tasks(list(self._background))

    # --- stream callbacks ----------------------------------------------------------

    def on_stream_status(self, connected: bool) -> None:
        self._stream_connected = connected
        metrics.private_ws_connected = connected
        if connected:
            self._stream_down_since = None
            if self._stream_alerted:
                self._stream_alerted = False
                self._spawn(self._send_alert("✅ Lifecycle monitor: private WS восстановлен."))
            # Events may have been missed while disconnected: REST is the source of truth.
            self._spawn(self.reconcile_all())
        elif self._stream_down_since is None:
            self._stream_down_since = time.monotonic()

    async def on_fills(self, fills: list[Fill]) -> None:
        for fill in fills:
            try:
                await self._on_fill(fill)
            except Exception as exc:
                self._error("fill", exc, symbol=fill.symbol)

    async def on_positions(self, states: list[PositionState]) -> None:
        for state in states:
            try:
                await self._on_position(state)
            except Exception as exc:
                self._error("position", exc, symbol=state.symbol)

    def notice_trade_opened(self, symbol: str) -> None:
        """Entry just persisted a fill: drop the negative lookup cache for the symbol."""
        self._misses.pop(symbol, None)

    async def on_protective_orders(self, orders: list[ProtectiveOrder]) -> None:
        for order in orders:
            tracked = self._tracked.get(order.symbol)
            if tracked is None or order.status not in {"Untriggered", "New"}:
                continue
            trade = tracked.trade
            if order.stop_order_type == "TakeProfit" and trade.tp_order_id is None:
                trade.tp_order_id = order.order_id
            elif order.stop_order_type == "StopLoss" and trade.sl_order_id is None:
                trade.sl_order_id = order.order_id
            else:
                continue
            if trade.status == ExecutionStatus.OPEN.value:
                try:
                    await self._store.mark_open(
                        trade.trade_id,
                        remaining_qty=tracked.remaining,
                        tp_order_id=trade.tp_order_id,
                        sl_order_id=trade.sl_order_id,
                    )
                except Exception as exc:
                    self._error("protective_ids", exc, symbol=order.symbol)

    def _buffer(self, fill: Fill) -> None:
        """Keep fills of not-yet-tracked symbols: the entry execution arrives on WS before
        TradingService has persisted the fill, and REST history may lag by seconds."""
        recent = self._recent.get(fill.symbol)
        if recent is None:
            if len(self._recent) >= _RECENT_SYMBOLS_MAX:
                self._recent.pop(next(iter(self._recent)))
            recent = self._recent[fill.symbol] = deque(maxlen=_RECENT_FILLS_PER_SYMBOL)
        recent.append(fill)

    async def _on_fill(self, fill: Fill) -> None:
        tracked = await self._ensure_tracked(fill.symbol)
        if tracked is None:
            self._buffer(fill)
            return
        if fill.exec_id in tracked.fills:
            return
        tracked.fills[fill.exec_id] = fill
        if not tracked.trade.is_closing_fill(fill):
            return
        self._log(
            logging.INFO,
            f"Closing execution {fill.symbol} qty={fill.closed_size} "
            f"stopOrderType={fill.stop_order_type or '-'} createType={fill.create_type or '-'}",
            tracked.trade,
            event="closing_fill",
            order_id=fill.order_id,
        )
        if tracked.zero_seen and tracked.evidence_complete():
            self._schedule_finalize(tracked, 0.0)

    async def _on_position(self, state: PositionState) -> None:
        tracked = await self._ensure_tracked(state.symbol)
        if tracked is None:
            return
        trade = tracked.trade
        size = state.size if state.side in (None, "", trade.side) else Decimal("0")
        if size > 0:
            await self._still_open(tracked, size)
            return
        if tracked.zero_seen:
            return
        tracked.zero_seen = True
        await self._store.mark_closing(trade.trade_id)
        trade.status = ExecutionStatus.CLOSING.value
        self._log(logging.INFO, f"Position size=0 {trade.symbol}", trade, event="position_zero")
        delay = 0.0 if tracked.evidence_complete() else self._cfg.correlation_window_sec
        self._schedule_finalize(tracked, delay)

    async def _still_open(self, tracked: _Tracked, size: Decimal) -> None:
        trade = tracked.trade
        if tracked.timer is not None:
            tracked.timer.cancel()
            tracked.timer = None
        tracked.zero_seen = False
        if trade.status not in (ExecutionStatus.OPEN.value,):
            await self._confirm_open(tracked, size)
        elif size != tracked.remaining:
            await self._store.update_remaining(trade.trade_id, size)
            if size < trade.qty:
                self._log(
                    logging.INFO,
                    f"Partial reduction {trade.symbol} remaining={size}",
                    trade,
                    event="partial_close",
                )
        tracked.remaining = size

    async def _confirm_open(self, tracked: _Tracked, size: Decimal) -> None:
        trade = tracked.trade
        if trade.tp_order_id is None or trade.sl_order_id is None:
            try:
                orders = await self._gw.get_protective_orders(trade.symbol)
            except Exception as exc:
                self._error("protective_orders", exc, symbol=trade.symbol)
                orders = []
            tps = [o.order_id for o in orders if o.stop_order_type == "TakeProfit"]
            sls = [o.order_id for o in orders if o.stop_order_type == "StopLoss"]
            if trade.tp_order_id is None and len(tps) == 1:
                trade.tp_order_id = tps[0]
            if trade.sl_order_id is None and len(sls) == 1:
                trade.sl_order_id = sls[0]
        await self._store.mark_open(
            trade.trade_id,
            remaining_qty=size,
            tp_order_id=trade.tp_order_id,
            sl_order_id=trade.sl_order_id,
        )
        trade.status = ExecutionStatus.OPEN.value
        self._log(logging.INFO, f"Monitoring open position {trade.symbol}", trade, event="open")

    # --- tracking ----------------------------------------------------------------------

    def _lock(self, symbol: str) -> asyncio.Lock:
        lock = self._locks.get(symbol)
        if lock is None:
            lock = self._locks[symbol] = asyncio.Lock()
        return lock

    def _track(self, trade: OpenTrade) -> _Tracked:
        tracked = self._tracked.get(trade.symbol)
        if tracked is None or tracked.trade.trade_id != trade.trade_id:
            tracked = _Tracked(trade=trade, remaining=trade.qty)
            self._tracked[trade.symbol] = tracked
            for fill in self._recent.pop(trade.symbol, ()):
                if fill.time_ms >= trade.opened_ms - _TS_LOOKBACK_MS:
                    tracked.fills.setdefault(fill.exec_id, fill)
        return tracked

    async def _ensure_tracked(self, symbol: str) -> _Tracked | None:
        tracked = self._tracked.get(symbol)
        if tracked is not None:
            return tracked
        missed = self._misses.get(symbol)
        if missed is not None and time.monotonic() - missed < _NEGATIVE_LOOKUP_TTL_SEC:
            return None
        trade = await self._store.find_tracked_trade(symbol, self._cfg.mode)
        if trade is None:
            self._misses[symbol] = time.monotonic()
            if len(self._misses) > 512:
                self._misses.clear()
            return None
        self._misses.pop(symbol, None)
        return self._track(trade)

    def _schedule_finalize(
        self, tracked: _Tracked, delay: float, *, recovered: bool = False
    ) -> None:
        if tracked.timer is not None:
            tracked.timer.cancel()
        symbol = tracked.trade.symbol
        loop = asyncio.get_running_loop()
        tracked.timer = loop.call_later(
            delay, lambda: self._spawn(self._finalize(symbol, recovered=recovered))
        )

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # --- finalize ---------------------------------------------------------------------

    async def _finalize(self, symbol: str, *, recovered: bool = False) -> None:
        try:
            async with self._lock(symbol):
                await self._finalize_locked(symbol, recovered=recovered)
        except Exception as exc:
            self._error("finalize", exc, symbol=symbol)

    async def _finalize_locked(self, symbol: str, *, recovered: bool) -> None:
        tracked = self._tracked.get(symbol)
        if tracked is None:
            return
        tracked.timer = None
        trade = tracked.trade

        try:
            position = await self._gw.get_position(symbol)
        except Exception as exc:
            self._error("get_position", exc, symbol=symbol)
            position = None
        if position is not None and position.size > 0 and position.side == trade.side:
            await self._still_open(tracked, position.size)
            return
        if position is None and not tracked.zero_seen:
            return  # size == 0 not confirmed by any source yet

        start_ms = trade.opened_ms - _TS_LOOKBACK_MS
        rest_ok = await self._merge_rest_fills(tracked, start_ms)
        # REST history can trail the position by seconds: bounded retry while the closing
        # executions do not yet cover the whole position.
        deadline = time.monotonic() + self._cfg.pnl_wait_sec
        while rest_ok and not tracked.evidence_complete() and time.monotonic() < deadline:
            await asyncio.sleep(min(1.0, self._cfg.pnl_wait_sec))
            rest_ok = await self._merge_rest_fills(tracked, start_ms)

        classification = classify_close(trade, tracked.fills.values())
        closing = sorted(tracked.closing_fills(), key=lambda f: (f.time_ms, f.seq))
        closed_at = (
            datetime.fromtimestamp(closing[-1].time_ms / 1000, UTC)
            if closing
            else datetime.now(UTC)
        )
        closed_qty = _sum(f.closed_size for f in closing)
        exit_price = (
            _sum(f.price * f.closed_size for f in closing) / closed_qty if closed_qty > 0 else None
        )
        realized = (
            _sum(f.pnl for f in closing if f.pnl is not None)
            if closing and all(f.pnl is not None for f in closing)
            else None
        )
        entry_fees = await self._entry_fees(tracked)
        fees = (
            entry_fees + _sum(f.fee for f in closing)
            if entry_fees is not None and closing
            else None
        )
        funding: Decimal | None = None
        if rest_ok:
            funding = _sum(
                f.fee
                for f in tracked.fills.values()
                if f.is_funding and start_ms <= f.time_ms <= int(closed_at.timestamp() * 1000)
            )
        net = (
            realized - fees - funding
            if realized is not None and fees is not None and funding is not None
            else None
        )
        # Bybit's own summary is a cross-check; it lags executions, so wait for it only
        # when the executions alone could not give a net result.
        bybit_closed = await self._bybit_closed_pnl(
            trade,
            closing,
            start_ms,
            wait_sec=0.0 if recovered or net is not None else self._cfg.pnl_wait_sec,
        )
        if bybit_closed is not None:
            if exit_price is None and bybit_closed.avg_exit_price:
                exit_price = bybit_closed.avg_exit_price
            if (
                fees is None
                and bybit_closed.open_fee is not None
                and bybit_closed.close_fee is not None
            ):
                fees = bybit_closed.open_fee + bybit_closed.close_fee

        # After a restart the current equity is not "balance after this close".
        balance: Decimal | None = None
        if not recovered:
            try:
                balance = await asyncio.wait_for(self._gw.get_equity(), _BALANCE_TIMEOUT_SEC)
            except Exception:
                balance = None

        event = TradeClosedEvent(
            trade_id=trade.trade_id,
            signal_id=trade.signal_id,
            symbol=trade.symbol,
            side=trade.side,
            mode=trade.mode,
            close_reason=classification.reason,
            reason_detail=classification.detail,
            entry_price=trade.entry_price,
            exit_price=exit_price,
            qty=trade.qty,
            closed_qty=closed_qty,
            opened_at=trade.opened_at,
            closed_at=closed_at,
            realized_pnl=realized,
            fees=fees,
            funding=funding,
            net_pnl=net,
            bybit_closed_pnl=bybit_closed.closed_pnl if bybit_closed is not None else None,
            balance_after=balance,
            close_order_id=classification.close_order_id,
            close_execution_id=classification.close_execution_id,
            recovered=recovered,
        )
        newly_closed = await self._store.finalize_close(event)
        self._tracked.pop(symbol, None)
        if not newly_closed:
            return
        reason = event.close_reason.value
        metrics.trade_closures_total[reason] = metrics.trade_closures_total.get(reason, 0) + 1
        self._last_closed = event
        self._log(
            logging.WARNING if reason in {"LIQUIDATION", "ADL", "UNKNOWN"} else logging.INFO,
            f"Trade closed {symbol} reason={reason} detail={event.reason_detail}",
            trade,
            event="trade_closed",
            order_id=event.close_order_id,
            close_reason=reason,
        )
        await self._deliver(event)

    async def _merge_rest_fills(self, tracked: _Tracked, start_ms: int) -> bool:
        symbol = tracked.trade.symbol
        try:
            fills = await self._gw.get_fills(symbol, start_ms, int(time.time() * 1000))
        except Exception as exc:
            self._error("get_fills", exc, symbol=symbol)
            return False
        for fill in fills:
            tracked.fills.setdefault(fill.exec_id, fill)
        return True

    async def _entry_fees(self, tracked: _Tracked) -> Decimal | None:
        trade = tracked.trade
        if not trade.entry_order_id:
            return None
        entry = [
            f for f in tracked.fills.values() if f.order_id == trade.entry_order_id and f.is_trade
        ]
        if not entry:
            try:
                entry = [
                    f
                    for f in await self._gw.get_order_fills(trade.symbol, trade.entry_order_id)
                    if f.is_trade
                ]
            except Exception as exc:
                self._error("entry_fills", exc, symbol=trade.symbol)
                return None
        return _sum(f.fee for f in entry) if entry else None

    async def _bybit_closed_pnl(
        self, trade: OpenTrade, closing: list[Fill], start_ms: int, *, wait_sec: float
    ) -> ClosedPnlRecord | None:
        """Bybit closed-PnL over *all* closing orders (one record each), or None if any is
        missing: a partial sum would misstate the result."""
        order_ids = {f.order_id for f in closing if f.order_id}
        if not order_ids:
            return None
        deadline = time.monotonic() + wait_sec
        while True:
            try:
                rows = await self._gw.get_closed_pnl(
                    trade.symbol, start_ms, int(time.time() * 1000)
                )
            except Exception as exc:
                self._error("closed_pnl", exc, symbol=trade.symbol)
                return None
            found = {row.order_id: row for row in rows if row.order_id in order_ids}
            if len(found) == len(order_ids):
                return _aggregate_closed_pnl(trade.symbol, list(found.values()))
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(min(1.0, wait_sec))

    # --- notification -------------------------------------------------------------------

    async def _deliver(self, event: TradeClosedEvent) -> None:
        stale = (
            event.recovered
            and datetime.now(UTC) - event.closed_at
            > timedelta(seconds=self._cfg.recovery_notify_max_age_sec)
        )
        if not await self._store.claim_close_notification(event.trade_id):
            return
        if stale or not self._cfg.notify_closes or self._notify is None:
            if stale:
                logger.info("Close of %s is too old to notify; recorded only", event.symbol)
            return
        try:
            delivered = await self._notify(event)
        except Exception:
            logger.warning("Close notification failed for %s", event.symbol, exc_info=True)
            delivered = False
        if delivered:
            metrics.close_notifications_sent_total += 1
            return
        metrics.close_notifications_failed_total += 1
        await self._store.release_close_notification(event.trade_id)

    async def _send_alert(self, text: str) -> None:
        if self._alert is None:
            return
        try:
            await self._alert(text)
        except Exception:
            logger.warning("Lifecycle alert failed", exc_info=True)

    # --- reconciliation -----------------------------------------------------------------

    async def _reconcile_loop(self) -> None:
        while True:
            await asyncio.sleep(self._cfg.reconcile_interval_sec)
            await self._check_stream()
            await self.reconcile_all()

    async def _check_stream(self) -> None:
        down = self._stream_down_since
        if (
            down is None
            or self._stream_alerted
            or time.monotonic() - down < self._cfg.ws_alert_after_sec
        ):
            return
        self._stream_alerted = True
        log(
            logger,
            logging.CRITICAL,
            "Bybit private WS down; lifecycle relies on REST reconciliation",
            component="trade_lifecycle",
            event="private_ws_down",
        )
        await self._send_alert(
            "🚨 Lifecycle monitor: private WS Bybit недоступен.\n"
            f"Закрытия сделок проверяются по REST раз в {self._cfg.reconcile_interval_sec:.0f}с.\n"
            "TP/SL на бирже не затронуты."
        )

    async def reconcile_all(self, *, recovered: bool = False) -> None:
        metrics.position_reconciliations_total += 1
        try:
            trades = await self._store.list_tracked_trades(self._cfg.mode)
            for trade in trades:
                self._track(trade)
            if self._tracked:
                positions = await self._gw.get_open_positions()
                for symbol, tracked in list(self._tracked.items()):
                    await self._reconcile_one(
                        symbol, tracked, positions.get(symbol), recovered=recovered
                    )
            since = datetime.now(UTC) - _RESEND_WINDOW
            for event in await self._store.list_unnotified_closed(self._cfg.mode, since):
                await self._deliver(event)
            self._last_reconcile_at = datetime.now(UTC)
        except Exception as exc:
            self._error("reconcile", exc)

    async def _reconcile_one(
        self,
        symbol: str,
        tracked: _Tracked,
        position: PositionState | None,
        *,
        recovered: bool,
    ) -> None:
        trade = tracked.trade
        async with self._lock(symbol):
            if self._tracked.get(symbol) is not tracked:
                return
            if position is not None and position.size > 0 and position.side == trade.side:
                await self._still_open(tracked, position.size)
                return
            if tracked.timer is not None:
                return  # a WS-triggered finalize is already scheduled
            if not tracked.zero_seen:
                tracked.zero_seen = True
                await self._store.mark_closing(trade.trade_id)
                trade.status = ExecutionStatus.CLOSING.value
                self._log(
                    logging.INFO,
                    f"Reconcile: position {symbol} is closed on Bybit",
                    trade,
                    event="reconcile_closed",
                )
        await self._finalize(symbol, recovered=recovered)

    # --- logging ------------------------------------------------------------------------

    def _error(self, where: str, exc: Exception, *, symbol: str | None = None) -> None:
        metrics.lifecycle_reconciliation_errors_total += 1
        self._last_error = f"{where}:{type(exc).__name__}"
        log(
            logger,
            logging.WARNING,
            f"Lifecycle {where} failed: {type(exc).__name__}",
            component="trade_lifecycle",
            event="lifecycle_error",
            symbol=symbol,
            reason=where,
            exc_info=True,
        )

    def _log(
        self,
        level: int,
        message: str,
        trade: OpenTrade,
        *,
        event: str,
        order_id: str | None = None,
        close_reason: str | None = None,
    ) -> None:
        log(
            logger,
            level,
            message,
            component="trade_lifecycle",
            event=event,
            symbol=trade.symbol,
            trade_id=str(trade.trade_id),
            side=trade.side,
            order_id=order_id or trade.entry_order_id,
            order_link_id=trade.order_link_id,
            close_reason=close_reason,
        )
