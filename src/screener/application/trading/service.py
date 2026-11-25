from __future__ import annotations

import asyncio
import html
import logging
import os
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from bybit_client import BybitGateway, OrderSide, create_bybit_http
from bybit_client.errors import (
    BybitApiError,
    BybitTransportError,
    ExecutionTimeoutError,
    OrderUncertainError,
)
from bybit_client.models import ResolvedInstrument, TakeProfitPlan
from screener.application.metrics import metrics
from screener.application.supervisor import cancel_tasks, supervise
from screener.application.trading.sizing import (
    SizeReject,
    clamp_leverage,
    compute_qty,
    protective_prices,
)
from screener.config import TradingSettings
from screener.domain.models import MarketSignal
from screener.domain.trading import ExecutionStatus, TradeAction
from screener.infrastructure.db.trading_repo import TradingRepository
from screener.logging import log

logger = logging.getLogger(__name__)

NotifyFunc = Callable[[str], Awaitable[None]]

_INSTRUMENT_CACHE_MAX = 256
_PREFLIGHT_RETRY_MIN_SEC = 30.0
_PREFLIGHT_RETRY_MAX_SEC = 600.0


@dataclass(slots=True)
class AccountStatsView:
    equity: Decimal
    wallet: Decimal
    available: Decimal
    unrealised_pnl: Decimal
    open_positions: int
    active_orders: int
    mode: str
    trading_enabled: bool
    trading_ready: bool
    ready_reason: str
    fetched_at: float
    disabled_reason: str = ""
    started_at: datetime | None = None
    instance: str = ""


_DISABLED_REASON_TEXT = {
    "not_started": "модуль ещё не запущен",
    "startup_default": "по умолчанию после запуска сервиса (TRADING_AUTO_RESUME=false)",
    "restart_no_auto_resume": (
        "сервис был перезапущен, а TRADING_AUTO_RESUME=false — "
        "торговля не восстанавливается автоматически"
    ),
    "state_load_failed": "не удалось прочитать сохранённое состояние из БД",
    "persisted_off": "сохранённое состояние: OFF",
    "telegram_off": "выключено кнопкой в Telegram",
    "queue_overflow": "переполнение очереди торговых сигналов",
}


class TradingService:
    """Optional MarketSignal → Bybit execution consumer."""

    def __init__(
        self,
        settings: TradingSettings,
        repo: TradingRepository,
        *,
        notify: NotifyFunc | None = None,
        gateway: BybitGateway | None = None,
    ) -> None:
        self.settings = settings
        self._repo = repo
        self._notify = notify
        self._gateway = gateway
        self._queue: asyncio.Queue[MarketSignal] = asyncio.Queue(
            maxsize=settings.trading_queue_maxsize
        )
        self._lock = asyncio.Lock()
        self._tasks: list[asyncio.Task] = []
        self._background: set[asyncio.Task] = set()
        self._enabled = False
        self._disabled_reason = "not_started"
        self._disabled_at: datetime | None = None
        self._started_at: datetime | None = None
        self._instance = f"{socket.gethostname()}:{os.getpid()}"
        self._ready = False
        self._ready_reason = "not_started"
        self._instrument_cache: dict[str, tuple[float, ResolvedInstrument]] = {}
        self._stats_cache: tuple[float, AccountStatsView] | None = None
        self._mainnet_confirm_until: float = 0.0
        self._position_listener: Callable[[str], None] | None = None

    @property
    def gateway(self) -> BybitGateway | None:
        return self._gateway

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def ready_reason(self) -> str:
        return self._ready_reason

    @property
    def disabled_reason(self) -> str:
        return "" if self._enabled else self._disabled_reason

    def describe_disabled_reason(self) -> str:
        if self._enabled:
            return ""
        text = _DISABLED_REASON_TEXT.get(self._disabled_reason, self._disabled_reason)
        if self._disabled_at is not None:
            text += f" ({self._disabled_at:%Y-%m-%d %H:%M:%S} UTC)"
        return text

    def _disable(self, reason: str) -> None:
        self._enabled = False
        self._disabled_reason = reason
        self._disabled_at = datetime.now(UTC)
        metrics.trading_enabled = False

    def set_notify(self, notify: NotifyFunc | None) -> None:
        self._notify = notify

    def set_position_listener(self, listener: Callable[[str], None] | None) -> None:
        """Called with the symbol right after an entry fill is persisted."""
        self._position_listener = listener

    def planned_action(self, signal: MarketSignal) -> str | None:
        """Side auto-trading will try for this signal, or ``None`` if it will not trade."""
        from screener.domain.exchanges import TRADING_EXCHANGE

        if getattr(signal, "exchange", TRADING_EXCHANGE) != TRADING_EXCHANGE:
            return None
        if not (self.settings.enabled_module and self._enabled and self._ready):
            return None
        action = self.settings.action_for(signal.signal_type.value)
        return None if action == TradeAction.IGNORE.value else action

    def enqueue(self, signal: MarketSignal) -> None:
        from screener.domain.exchanges import TRADING_EXCHANGE

        if getattr(signal, "exchange", TRADING_EXCHANGE) != TRADING_EXCHANGE:
            metrics.trade_signals_foreign_exchange_total += 1
            return
        if not self.settings.enabled_module:
            return
        metrics.trade_signals_received_total += 1
        if self._queue.full():
            log(
                logger,
                logging.CRITICAL,
                "Trading queue overflow; disabling new entries",
                component="trading",
                event="queue_overflow",
                symbol=signal.symbol,
                signal_id=str(signal.id),
            )
            already_off = not self._enabled and self._disabled_reason == "queue_overflow"
            self._disable("queue_overflow")
            self._ready = False
            self._ready_reason = "queue_overflow"
            metrics.trading_ready = False
            if not already_off:
                task = asyncio.get_running_loop().create_task(
                    self._notify_text(
                        "🚨 Торговля выключена: переполнение очереди сигналов.\n"
                        "Новые входы запрещены до перезапуска сервиса."
                    )
                )
                self._background.add(task)
                task.add_done_callback(self._background.discard)
            return
        log(
            logger,
            logging.DEBUG,
            f"Trading enqueue {signal.symbol} qsize={self._queue.qsize() + 1}",
            component="trading",
            event="enqueue",
            symbol=signal.symbol,
            signal_id=str(signal.id),
            signal_type=signal.signal_type.value,
        )
        self._queue.put_nowait(signal)

    async def start(self) -> None:
        if not self.settings.enabled_module:
            self._ready = False
            self._ready_reason = "disabled"
            return
        if not self.settings.orders_environment_ok():
            self._ready = False
            self._ready_reason = "credentials_or_mainnet_gate"
            logger.warning("Trading module not ready: %s", self._ready_reason)
            metrics.trading_ready = False
            self._tasks.append(supervise("trading-worker", self._loop))
            return

        if self._gateway is None:
            http = create_bybit_http(
                api_key=self.settings.bybit_api_key,
                api_secret=self.settings.bybit_api_secret,
                demo=self.settings.is_demo,
            )
            self._gateway = BybitGateway(
                http,
                demo=self.settings.is_demo,
                api_key=self.settings.bybit_api_key,
                api_secret=self.settings.bybit_api_secret,
            )
        try:
            await self._gateway.start()
            await self._preflight()
            self._ready = True
            self._ready_reason = "ok"
        except Exception as exc:
            self._ready = False
            self._ready_reason = f"preflight:{exc}"
            logger.exception("Trading preflight failed")
            metrics.record_error()

        self._started_at = datetime.now(UTC)
        persisted: bool | None
        try:
            persisted = await self._repo.get_runtime_enabled()
        except Exception:
            logger.warning("Could not load trading runtime state", exc_info=True)
            persisted = None

        if persisted is None:
            self._disable("state_load_failed")
        elif not self.settings.trading_auto_resume:
            self._disable("restart_no_auto_resume" if persisted else "startup_default")
        elif persisted:
            self._enabled = True
            metrics.trading_enabled = True
        else:
            self._disable("persisted_off")

        metrics.trading_ready = self._ready
        self._tasks.append(supervise("trading-worker", self._loop))
        if not self._ready:
            task = asyncio.get_running_loop().create_task(
                self._preflight_retry_loop(), name="trading-preflight-retry"
            )
            self._background.add(task)
            task.add_done_callback(self._background.discard)
        log(
            logger,
            logging.WARNING if persisted and not self._enabled else logging.INFO,
            f"Trading module started mode={self.settings.bybit_trading_mode} "
            f"ready={self._ready} enabled={self._enabled} persisted={persisted} "
            f"auto_resume={self.settings.trading_auto_resume} "
            f"disabled_reason={self.disabled_reason or '-'} instance={self._instance}",
            component="trading",
            event="started",
            status="on" if self._enabled else "off",
            reason=self.disabled_reason or None,
        )

    async def stop(self) -> None:
        await cancel_tasks(self._tasks)
        await cancel_tasks(list(self._background))
        if self._gateway is not None:
            await self._gateway.close()

    async def _preflight_retry_loop(self) -> None:
        """A Bybit blip at boot must not leave trading unusable until the next restart."""
        delay = _PREFLIGHT_RETRY_MIN_SEC
        while not self._ready and self._ready_reason.startswith("preflight:"):
            await asyncio.sleep(delay)
            delay = min(delay * 2, _PREFLIGHT_RETRY_MAX_SEC)
            try:
                await self._preflight()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._ready_reason = f"preflight:{exc}"
                logger.warning("Trading preflight retry failed: %s", exc)
                continue
            self._ready = True
            self._ready_reason = "ok"
            metrics.trading_ready = True
            logger.info("Trading preflight succeeded on retry")
            await self._notify_text(
                "✅ Торговый модуль готов (проверка Bybit прошла после повторной попытки).\n"
                f"Trading: {'ON' if self._enabled else 'OFF'}"
            )

    async def _preflight(self) -> None:
        assert self._gateway is not None
        info = await self._gateway.get_api_key_info()
        if info.get("readOnly"):
            raise RuntimeError("api_key_readonly")
        await self._gateway.get_balance()
        await self._gateway.assert_one_way_mode()

    async def set_enabled(self, enabled: bool, *, chat_id: int | None = None) -> str | None:
        """Returns error reason or None on success."""
        if enabled:
            if not self._ready:
                return self._ready_reason or "not_ready"
            if self.settings.bybit_trading_mode == "mainnet":
                if not self.settings.allow_mainnet_trading:
                    return "mainnet_not_allowed"
                if time.monotonic() > self._mainnet_confirm_until:
                    return "mainnet_confirm_required"
            if not self.settings.orders_environment_ok():
                return "environment_blocked"
        if enabled:
            self._enabled = True
            metrics.trading_enabled = True
            self._mainnet_confirm_until = 0.0  # one confirmation = one enable
        else:
            self._disable("telegram_off")
        log(
            logger,
            logging.INFO,
            f"Trading enabled={enabled} by chat_id={chat_id}",
            component="trading",
            event="enabled_changed",
            status="on" if enabled else "off",
        )
        try:
            await self._repo.set_runtime_enabled(enabled, chat_id=chat_id)
        except Exception:
            log(
                logger,
                logging.WARNING,
                "Failed to persist trading runtime state",
                component="trading",
                event="runtime_persist_failed",
                exc_info=True,
            )
        return None

    def arm_mainnet_confirm(self, ttl_sec: float = 60.0) -> None:
        self._mainnet_confirm_until = time.monotonic() + ttl_sec

    def mainnet_confirm_armed(self) -> bool:
        return time.monotonic() <= self._mainnet_confirm_until

    async def get_account_stats(self, *, force: bool = False) -> AccountStatsView:
        ttl = self.settings.account_stats_cache_ttl_sec
        if (
            not force
            and self._stats_cache is not None
            and time.monotonic() - self._stats_cache[0] < ttl
        ):
            return self._stats_cache[1]
        view = await self._fetch_stats()
        self._stats_cache = (time.monotonic(), view)
        return view

    async def _fetch_stats(self) -> AccountStatsView:
        if self._gateway is None or not self._ready:
            return AccountStatsView(
                equity=Decimal("0"),
                wallet=Decimal("0"),
                available=Decimal("0"),
                unrealised_pnl=Decimal("0"),
                open_positions=0,
                active_orders=0,
                mode=self.settings.bybit_trading_mode,
                trading_enabled=self._enabled,
                trading_ready=self._ready,
                ready_reason=self._ready_reason,
                fetched_at=time.monotonic(),
                disabled_reason=self.describe_disabled_reason(),
                started_at=self._started_at,
                instance=self._instance,
            )
        bal = await self._gateway.get_balance()
        positions = await self._gateway.list_open_positions()
        orders = await self._gateway.count_open_orders()
        metrics.open_positions = len(positions)
        metrics.active_orders = orders
        metrics.last_private_api_success_ts = time.time()
        return AccountStatsView(
            equity=bal.total_equity,
            wallet=bal.total_wallet_balance,
            available=bal.total_available_balance,
            unrealised_pnl=bal.total_perp_upl,
            open_positions=len(positions),
            active_orders=orders,
            mode=self.settings.bybit_trading_mode,
            trading_enabled=self._enabled,
            trading_ready=self._ready,
            ready_reason=self._ready_reason,
            fetched_at=time.monotonic(),
            disabled_reason=self.describe_disabled_reason(),
            started_at=self._started_at,
            instance=self._instance,
        )

    async def _loop(self) -> None:
        while True:
            signal = await self._queue.get()
            try:
                await self._handle(signal)
            except Exception:
                metrics.record_error()
                metrics.orders_failed_total += 1
                logger.exception("Trading handle failed for %s", signal.symbol)
            finally:
                self._queue.task_done()

    async def _handle(self, signal: MarketSignal) -> None:
        action = TradeAction(self.settings.action_for(signal.signal_type.value))
        log(
            logger,
            logging.INFO,
            f"Trading handle {signal.symbol} type={signal.signal_type.value} "
            f"action={action.value} enabled={self._enabled} ready={self._ready}",
            component="trading",
            event="handle_start",
            symbol=signal.symbol,
            signal_id=str(signal.id),
            signal_type=signal.signal_type.value,
            action=action.value,
        )
        if action is TradeAction.IGNORE:
            metrics.trade_signals_ignored_total += 1
            log(
                logger,
                logging.INFO,
                f"Trading ignore {signal.symbol} (action=IGNORE)",
                component="trading",
                event="ignored",
                symbol=signal.symbol,
                signal_id=str(signal.id),
                signal_type=signal.signal_type.value,
                action=action.value,
            )
            return

        if not self._enabled:
            await self._skip(signal, action, "disabled", reserve=False)
            return
        if not self._ready:
            await self._skip(signal, action, "not_ready", reserve=False)
            return
        if not self.settings.orders_environment_ok():
            await self._skip(signal, action, "environment_blocked", reserve=False)
            return

        age = (datetime.now(UTC) - signal.detected_at).total_seconds()
        if age > self.settings.max_signal_age_seconds:
            await self._skip(signal, action, "signal_too_old", reserve=True)
            return

        side = OrderSide.BUY if action is TradeAction.LONG else OrderSide.SELL
        order_link_id = str(signal.id)

        async with self._lock:
            reserved = await self._repo.try_reserve(
                signal_id=signal.id,
                symbol=signal.symbol,
                signal_type=signal.signal_type.value,
                configured_action=action.value,
                side=side.value,
                mode=self.settings.bybit_trading_mode,
                order_link_id=order_link_id,
            )
            if not reserved:
                log(
                    logger,
                    logging.WARNING,
                    f"Duplicate trading signal skipped id={signal.id}",
                    component="trading",
                    event="duplicate_signal",
                    symbol=signal.symbol,
                    signal_id=str(signal.id),
                )
                return

            assert self._gateway is not None
            try:
                await self._execute(signal, action, side, order_link_id)
            except Exception as exc:
                await self._repo.update_execution(
                    signal.id,
                    status=ExecutionStatus.FAILED.value,
                    error_code=type(exc).__name__,
                    error_message=str(exc)[:500],
                )
                metrics.orders_failed_total += 1
                log(
                    logger,
                    logging.ERROR,
                    f"Trading execution failed {signal.symbol}: {type(exc).__name__}",
                    component="trading",
                    event="execution_failed",
                    symbol=signal.symbol,
                    signal_id=str(signal.id),
                    reason=type(exc).__name__,
                    exc_info=True,
                )
                await self._notify_text(
                    f"❌ Ошибка исполнения\n{signal.symbol}\n{type(exc).__name__}"
                )
                raise

    async def _skip(
        self,
        signal: MarketSignal,
        action: TradeAction,
        reason: str,
        *,
        reserve: bool,
    ) -> None:
        metrics.trade_signals_skipped_total += 1
        detail = ""
        if reason == "disabled":
            detail = self.describe_disabled_reason()
        elif reason == "not_ready":
            detail = self._ready_reason
        log(
            logger,
            logging.INFO,
            f"Trading skip {signal.symbol} reason={reason} action={action.value}"
            + (f" detail={detail}" if detail else ""),
            component="trading",
            event="skipped",
            symbol=signal.symbol,
            signal_id=str(signal.id),
            signal_type=signal.signal_type.value,
            action=action.value,
            reason=reason,
        )
        if reserve:
            side = (
                OrderSide.BUY.value
                if action is TradeAction.LONG
                else OrderSide.SELL.value
                if action is not TradeAction.IGNORE
                else None
            )
            ok = await self._repo.try_reserve(
                signal_id=signal.id,
                symbol=signal.symbol,
                signal_type=signal.signal_type.value,
                configured_action=action.value,
                side=side,
                mode=self.settings.bybit_trading_mode,
                order_link_id=str(signal.id),
            )
            if ok:
                await self._repo.update_execution(
                    signal.id,
                    status=ExecutionStatus.SKIPPED.value,
                    skip_reason=reason,
                )

    async def _instrument(self, symbol: str) -> ResolvedInstrument:
        assert self._gateway is not None
        cached = self._instrument_cache.get(symbol)
        now = time.monotonic()
        if cached and now - cached[0] < self.settings.instrument_cache_ttl_sec:
            return cached[1]
        inst = await self._gateway.get_instrument(symbol)
        if inst is None:
            raise BybitApiError(-1, "instrument_not_found")
        self._instrument_cache.pop(symbol, None)
        self._instrument_cache[symbol] = (now, inst)
        while len(self._instrument_cache) > _INSTRUMENT_CACHE_MAX:
            self._instrument_cache.pop(next(iter(self._instrument_cache)))
        return inst

    async def _execute(
        self,
        signal: MarketSignal,
        action: TradeAction,
        side: OrderSide,
        order_link_id: str,
    ) -> None:
        assert self._gateway is not None
        gw = self._gateway
        instrument = await self._instrument(signal.symbol)
        pos = await gw.get_position(signal.symbol)
        if pos.size > 0:
            await self._repo.update_execution(
                signal.id,
                status=ExecutionStatus.SKIPPED.value,
                skip_reason="existing_position",
            )
            metrics.trade_signals_skipped_total += 1
            return
        # Bybit may already be flat while the lifecycle monitor has not closed the old row
        # yet; a new entry would then be finalized with the old trade's fills.
        if await self._repo.find_tracked_trade(signal.symbol, self.settings.bybit_trading_mode):
            await self._repo.update_execution(
                signal.id,
                status=ExecutionStatus.SKIPPED.value,
                skip_reason="previous_trade_not_closed",
            )
            metrics.trade_signals_skipped_total += 1
            return

        open_positions = await gw.list_open_positions()
        if len(open_positions) >= self.settings.max_open_positions:
            await self._repo.update_execution(
                signal.id,
                status=ExecutionStatus.SKIPPED.value,
                skip_reason="max_positions",
            )
            metrics.trade_signals_skipped_total += 1
            return

        bal = await gw.get_balance()
        ticker = await gw.get_ticker(signal.symbol)
        price = (
            ticker.mark_price
            if self.settings.order_sizing_price == "markPrice"
            else ticker.last_price
        )
        leverage = clamp_leverage(Decimal(str(self.settings.bybit_leverage)), instrument)
        sized = compute_qty(
            equity=bal.total_equity,
            margin_pct=Decimal(str(self.settings.trade_margin_pct)),
            leverage=leverage,
            sizing_price=price,
            instrument=instrument,
        )
        if isinstance(sized, SizeReject):
            await self._repo.update_execution(
                signal.id,
                status=ExecutionStatus.SKIPPED.value,
                skip_reason=sized.reason,
                leverage=float(leverage),
            )
            metrics.trade_signals_skipped_total += 1
            return

        if pos.leverage is None or pos.leverage != leverage:
            await gw.set_leverage(signal.symbol, leverage)

        await self._repo.update_execution(
            signal.id,
            requested_qty=float(sized.qty),
            leverage=float(leverage),
            status=ExecutionStatus.SUBMITTED.value,
        )

        try:
            submission = await gw.place_order(
                symbol=signal.symbol,
                side=side,
                qty=sized.qty,
                order_link_id=order_link_id,
            )
            metrics.orders_submitted_total += 1
        except OrderUncertainError:
            await self._repo.update_execution(
                signal.id, status=ExecutionStatus.RECONCILING.value
            )
            found = await gw.get_order_by_link_id(signal.symbol, order_link_id)
            if found is None:
                await self._repo.update_execution(
                    signal.id,
                    status=ExecutionStatus.FAILED.value,
                    error_code="uncertain_no_order",
                    error_message="place_order uncertain and order not found",
                )
                metrics.orders_failed_total += 1
                await self._notify_text(
                    f"❌ Ошибка исполнения\n{signal.symbol}\nuncertain_no_order"
                )
                return
            submission_order_id = found.order_id
            fill = found
        else:
            submission_order_id = submission.order_id
            try:
                fill = await gw.wait_for_fill(
                    symbol=signal.symbol,
                    order_link_id=order_link_id,
                    timeout_sec=self.settings.fill_timeout_sec,
                )
            except ExecutionTimeoutError:
                await self._repo.update_execution(
                    signal.id, status=ExecutionStatus.RECONCILING.value
                )
                found = await gw.get_order_by_link_id(signal.symbol, order_link_id)
                if found is None:
                    await self._repo.update_execution(
                        signal.id,
                        status=ExecutionStatus.FAILED.value,
                        bybit_order_id=submission_order_id,
                        error_code="fill_timeout",
                    )
                    metrics.orders_failed_total += 1
                    await self._notify_text(
                        f"❌ Ошибка исполнения\n{signal.symbol}\nfill_timeout"
                    )
                    return
                fill = found

        status_l = fill.status.lower()
        if status_l in {"rejected", "cancelled", "deactivated"}:
            await self._repo.update_execution(
                signal.id,
                status=ExecutionStatus.REJECTED.value,
                bybit_order_id=fill.order_id or submission_order_id,
                filled_qty=float(fill.filled_qty),
                avg_fill_price=float(fill.avg_price) if fill.avg_price else None,
                error_message=fill.status,
            )
            metrics.orders_failed_total += 1
            await self._notify_text(
                f"❌ Ошибка исполнения\n{signal.symbol}\n{fill.status}"
            )
            return

        filled = fill.filled_qty > 0 and fill.avg_price is not None
        exec_status = (
            ExecutionStatus.FILLED
            if status_l == "filled" or (filled and status_l not in {"new", "created"})
            else ExecutionStatus.PARTIALLY_FILLED
            if filled
            else ExecutionStatus.SUBMITTED
        )
        await self._repo.update_execution(
            signal.id,
            status=exec_status.value,
            bybit_order_id=fill.order_id or submission_order_id,
            filled_qty=float(fill.filled_qty),
            avg_fill_price=float(fill.avg_price) if fill.avg_price else None,
        )

        if not filled or fill.avg_price is None:
            await self._notify_text(
                f"❌ Ошибка исполнения\n{signal.symbol}\nno_fill_yet:{fill.status}"
            )
            metrics.orders_failed_total += 1
            return

        metrics.orders_filled_total += 1
        if self._position_listener is not None:
            try:
                self._position_listener(signal.symbol)
            except Exception:
                logger.warning("Position listener failed", exc_info=True)
        tp_pct = (
            Decimal(str(self.settings.take_profit_pct))
            if self.settings.take_profit_enabled
            else None
        )
        sl_pct = (
            Decimal(str(self.settings.stop_loss_pct))
            if self.settings.stop_loss_enabled
            else None
        )
        tp, sl = protective_prices(
            side=side.value,
            avg_fill=fill.avg_price,
            take_profit_pct=tp_pct,
            stop_loss_pct=sl_pct,
            tick_size=instrument.tick_size,
        )
        protective_ok = True
        if tp is not None or sl is not None:
            plan = TakeProfitPlan(
                take_profit_price=tp,
                stop_loss_price=sl,
                tp_trigger_by=self.settings.tp_trigger_by,
                sl_trigger_by=self.settings.sl_trigger_by if sl is not None else None,
            )
            try:
                await gw.set_trading_stop(symbol=signal.symbol, plan=plan)
            except (BybitApiError, BybitTransportError) as exc:
                protective_ok = False
                logger.critical(
                    "Failed to set TP/SL for %s after fill: %s", signal.symbol, exc
                )
                await self._repo.update_execution(
                    signal.id,
                    status=ExecutionStatus.PROTECTIVE_FAILED.value,
                    take_profit=float(tp) if tp else None,
                    stop_loss=float(sl) if sl else None,
                    error_code="protective_failed",
                    error_message=str(exc)[:500],
                )
                await self._notify_text(
                    f"❌ Ошибка исполнения\n{signal.symbol}\n"
                    f"Позиция открыта, но TP/SL не установлены\n{html.escape(str(exc)[:500])}"
                )

        if protective_ok:
            await self._repo.update_execution(
                signal.id,
                take_profit=float(tp) if tp else None,
                stop_loss=float(sl) if sl else None,
            )
            await self._notify_text(
                "✅ Сделка открыта\n"
                f"{signal.symbol}\n"
                f"Signal: {signal.signal_type.value}\n"
                f"Action: {action.value}\n"
                f"Side: {side.value}\n"
                f"Qty: {fill.filled_qty}\n"
                f"Entry: {fill.avg_price}\n"
                f"Leverage: {leverage}x\n"
                f"TP: {tp if tp is not None else '—'}\n"
                f"SL: {sl if sl is not None else '—'}"
            )
        self._stats_cache = None

    async def _notify_text(self, text: str) -> None:
        if self._notify is None:
            return
        try:
            await self._notify(text)
        except Exception:
            logger.warning("Trading notify failed", exc_info=True)
