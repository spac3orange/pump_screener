from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from bybit_client.models import (
    AccountSnapshot,
    MarketTicker,
    OrderExecution,
    OrderSubmission,
    PositionSnapshot,
    ResolvedInstrument,
)
from screener.application.metrics import metrics
from screener.application.trading.service import TradingService
from screener.config import TradingSettings
from screener.domain.enums import Direction, HistoryQuality, SignalType
from screener.domain.models import MarketSignal, RuleMatch
from screener.domain.trading import ExecutionStatus


def _settings(**kwargs) -> TradingSettings:
    base = dict(
        bybit_trading_mode="demo",
        bybit_api_key="k",
        bybit_api_secret="s",
        pump_action="SHORT",
        drop_action="IGNORE",
        dump_action="LONG",
        trading_notify_skips=True,
        max_signal_age_seconds=60,
        _env_file=None,
    )
    base.update(kwargs)
    return TradingSettings(**base)


def _instrument() -> ResolvedInstrument:
    return ResolvedInstrument(
        symbol="BTCUSDT",
        status="Trading",
        contract_type="LinearPerpetual",
        base_coin="BTC",
        quote_coin="USDT",
        settle_coin="USDT",
        min_leverage=Decimal("1"),
        max_leverage=Decimal("50"),
        leverage_step=Decimal("1"),
        min_notional_value=Decimal("5"),
        min_order_qty=Decimal("0.001"),
        qty_step=Decimal("0.001"),
        max_mkt_order_qty=Decimal("10"),
        tick_size=Decimal("0.1"),
    )


def _signal(signal_type: SignalType = SignalType.PUMP, symbol: str = "BTCUSDT") -> MarketSignal:
    match = RuleMatch(
        signal_type=signal_type,
        interval_sec=720,
        threshold_pct=40,
        change_pct=45 if signal_type is SignalType.PUMP else -45,
        prev_price=100,
        price=145 if signal_type is SignalType.PUMP else 55,
        detected_ts_ms=int(datetime.now(UTC).timestamp() * 1000),
        history_quality=HistoryQuality.LIVE,
    )
    return MarketSignal(
        symbol=symbol,
        direction=Direction.UP if signal_type is SignalType.PUMP else Direction.DOWN,
        primary=match,
        matches=(match,),
        detected_at=datetime.now(UTC),
        rule_version="test",
        id=uuid4(),
    )


class FakeRepo:
    def __init__(self) -> None:
        self.reserved: set = set()
        self.updates: list[tuple] = []
        self.enabled = False

    async def try_reserve(self, *, signal_id, **kwargs) -> bool:
        if signal_id in self.reserved:
            return False
        self.reserved.add(signal_id)
        return True

    async def update_execution(self, signal_id, **fields) -> None:
        self.updates.append((signal_id, fields))

    async def get_runtime_enabled(self) -> bool:
        return self.enabled

    async def set_runtime_enabled(self, enabled: bool, *, chat_id=None) -> None:
        self.enabled = enabled


class FakeGateway:
    def __init__(self) -> None:
        self.place_calls = 0
        self.orders: dict[str, OrderExecution] = {}
        self.positions: dict[str, PositionSnapshot] = {}
        self.open_positions: list[PositionSnapshot] = []
        self.fail_place_uncertain = False
        self.tp_sl_calls = 0

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def get_api_key_info(self) -> dict:
        return {"readOnly": False, "id": "1"}

    async def get_balance(self) -> AccountSnapshot:
        return AccountSnapshot(
            total_equity=Decimal("1000"),
            total_available_balance=Decimal("900"),
            margin_mode="REGULAR",
            unified_margin_status=3,
            total_wallet_balance=Decimal("980"),
            total_perp_upl=Decimal("20"),
        )

    async def assert_one_way_mode(self) -> None:
        return None

    async def get_instrument(self, symbol: str) -> ResolvedInstrument:
        return _instrument()

    async def get_ticker(self, symbol: str) -> MarketTicker:
        return MarketTicker(symbol=symbol, last_price=Decimal("100"), mark_price=Decimal("100"))

    async def get_position(self, symbol: str) -> PositionSnapshot:
        empty = PositionSnapshot(
            symbol=symbol, size=Decimal("0"), side=None, avg_price=None, leverage=None
        )
        return self.positions.get(symbol, empty)

    async def list_open_positions(self) -> list[PositionSnapshot]:
        return list(self.open_positions)

    async def count_open_orders(self) -> int:
        return 0

    async def set_leverage(self, symbol: str, leverage: Decimal) -> None:
        return None

    async def place_order(self, *, symbol, side, qty, order_link_id, **kwargs) -> OrderSubmission:
        self.place_calls += 1
        if self.fail_place_uncertain:
            from bybit_client.errors import OrderUncertainError

            raise OrderUncertainError("timeout")
        fill = OrderExecution(
            order_id="oid-1",
            order_link_id=order_link_id,
            symbol=symbol,
            side=side,
            qty=qty,
            filled_qty=qty,
            avg_price=Decimal("100"),
            status="Filled",
        )
        self.orders[order_link_id] = fill
        return OrderSubmission(
            order_id="oid-1",
            order_link_id=order_link_id,
            symbol=symbol,
            side=side,
            qty=qty,
            status="Created",
        )

    async def get_order_by_link_id(self, symbol: str, order_link_id: str) -> OrderExecution | None:
        return self.orders.get(order_link_id)

    async def wait_for_fill(self, *, symbol, order_link_id, timeout_sec) -> OrderExecution:
        return self.orders[order_link_id]

    async def set_trading_stop(self, *, symbol, plan) -> None:
        self.tp_sl_calls += 1


@pytest.fixture(autouse=True)
def _reset_metrics() -> None:
    metrics.__init__()


async def _run_one(service: TradingService, signal: MarketSignal) -> None:
    service.enqueue(signal)
    await asyncio.wait_for(service._handle(signal), timeout=5)
    # drain queue item if put
    try:
        service._queue.get_nowait()
        service._queue.task_done()
    except asyncio.QueueEmpty:
        pass


@pytest.mark.asyncio
async def test_pump_maps_to_short_sell() -> None:
    gw = FakeGateway()
    repo = FakeRepo()
    notes: list[str] = []

    async def notify(text: str) -> None:
        notes.append(text)

    svc = TradingService(_settings(), repo, notify=notify, gateway=gw)  # type: ignore[arg-type]
    svc._ready = True
    svc._enabled = True
    sig = _signal(SignalType.PUMP)
    await svc._handle(sig)
    assert gw.place_calls == 1
    assert any("SHORT" in n and "Sell" in n for n in notes)
    assert any(u[1].get("status") == ExecutionStatus.FILLED.value for u in repo.updates)


@pytest.mark.asyncio
async def test_ignore_action_no_order() -> None:
    gw = FakeGateway()
    svc = TradingService(_settings(drop_action="IGNORE"), FakeRepo(), gateway=gw)  # type: ignore[arg-type]
    svc._ready = True
    svc._enabled = True
    await svc._handle(_signal(SignalType.DROP))
    assert gw.place_calls == 0
    assert metrics.trade_signals_ignored_total == 1


@pytest.mark.asyncio
async def test_trading_off_skips_without_order() -> None:
    gw = FakeGateway()
    notes: list[str] = []

    async def notify(text: str) -> None:
        notes.append(text)

    svc = TradingService(_settings(), FakeRepo(), notify=notify, gateway=gw)  # type: ignore[arg-type]
    svc._ready = True
    svc._enabled = False
    await svc._handle(_signal())
    assert gw.place_calls == 0
    assert any("disabled" in n for n in notes)


@pytest.mark.asyncio
async def test_mainnet_without_allow_blocks_enable() -> None:
    svc = TradingService(
        _settings(bybit_trading_mode="mainnet", allow_mainnet_trading=False),
        FakeRepo(),
        gateway=FakeGateway(),  # type: ignore[arg-type]
    )
    svc._ready = True
    err = await svc.set_enabled(True)
    assert err == "mainnet_not_allowed"


@pytest.mark.asyncio
async def test_duplicate_signal_id_one_order() -> None:
    gw = FakeGateway()
    repo = FakeRepo()
    svc = TradingService(_settings(), repo, gateway=gw)  # type: ignore[arg-type]
    svc._ready = True
    svc._enabled = True
    sig = _signal()
    await svc._handle(sig)
    await svc._handle(sig)
    assert gw.place_calls == 1


@pytest.mark.asyncio
async def test_existing_position_skips() -> None:
    gw = FakeGateway()
    gw.positions["BTCUSDT"] = PositionSnapshot(
        symbol="BTCUSDT",
        size=Decimal("1"),
        side="Buy",
        avg_price=Decimal("100"),
        leverage=Decimal("3"),
    )
    notes: list[str] = []

    async def notify(text: str) -> None:
        notes.append(text)

    svc = TradingService(_settings(), FakeRepo(), notify=notify, gateway=gw)  # type: ignore[arg-type]
    svc._ready = True
    svc._enabled = True
    await svc._handle(_signal())
    assert gw.place_calls == 0
    assert any("existing_position" in n for n in notes)


@pytest.mark.asyncio
async def test_max_positions_skips() -> None:
    gw = FakeGateway()
    gw.open_positions = [
        PositionSnapshot(
            symbol="ETHUSDT",
            size=Decimal("1"),
            side="Buy",
            avg_price=Decimal("1"),
            leverage=Decimal("1"),
        )
    ]
    notes: list[str] = []

    async def notify(text: str) -> None:
        notes.append(text)

    svc = TradingService(
        _settings(max_open_positions=1), FakeRepo(), notify=notify, gateway=gw
    )  # type: ignore[arg-type]
    svc._ready = True
    svc._enabled = True
    await svc._handle(_signal())
    assert gw.place_calls == 0
    assert any("max_positions" in n for n in notes)


@pytest.mark.asyncio
async def test_timeout_reconciles_without_second_create() -> None:
    gw = FakeGateway()
    gw.fail_place_uncertain = True
    link = None

    async def place_order(**kwargs):
        nonlocal link
        link = kwargs["order_link_id"]
        gw.place_calls += 1
        from bybit_client.errors import OrderUncertainError

        gw.orders[link] = OrderExecution(
            order_id="oid-x",
            order_link_id=link,
            symbol=kwargs["symbol"],
            side=kwargs["side"],
            qty=kwargs["qty"],
            filled_qty=kwargs["qty"],
            avg_price=Decimal("100"),
            status="Filled",
        )
        raise OrderUncertainError("timeout")

    gw.place_order = place_order  # type: ignore[method-assign]
    svc = TradingService(_settings(), FakeRepo(), gateway=gw)  # type: ignore[arg-type]
    svc._ready = True
    svc._enabled = True
    await svc._handle(_signal())
    assert gw.place_calls == 1


def test_enqueue_ignores_non_bybit_signals() -> None:
    svc = TradingService(_settings(), FakeRepo(), gateway=FakeGateway())  # type: ignore[arg-type]
    svc._ready = True
    svc._enabled = True
    match = RuleMatch(
        signal_type=SignalType.PUMP,
        interval_sec=720,
        threshold_pct=40,
        change_pct=45,
        prev_price=100,
        price=145,
        detected_ts_ms=int(datetime.now(UTC).timestamp() * 1000),
        history_quality=HistoryQuality.LIVE,
    )
    sig = MarketSignal(
        symbol="BTCUSDT",
        direction=Direction.UP,
        primary=match,
        matches=(match,),
        detected_at=datetime.now(UTC),
        rule_version="test",
        exchange="binance",
    )
    svc.enqueue(sig)
    assert svc._queue.empty()
    assert metrics.trade_signals_foreign_exchange_total == 1
    assert metrics.trade_signals_received_total == 0


@pytest.mark.asyncio
async def test_dispatch_enqueues_without_blocking() -> None:

    gw = FakeGateway()
    repo = FakeRepo()
    svc = TradingService(_settings(), repo, gateway=gw)  # type: ignore[arg-type]
    svc._ready = True
    svc._enabled = False
    state = SimpleNamespace(
        persistence=SimpleNamespace(enqueue_signal=lambda s: None),
        notifications=SimpleNamespace(enqueue=lambda s: None),
        trading=svc,
    )
    # mimic AppState.dispatch_signal
    sig = _signal()
    state.persistence.enqueue_signal(sig)
    state.notifications.enqueue(sig)
    if state.trading is not None:
        state.trading.enqueue(sig)
    assert svc._queue.qsize() == 1


@pytest.mark.asyncio
async def test_successful_trade_keeps_trading_enabled() -> None:
    gw = FakeGateway()
    svc = TradingService(_settings(), FakeRepo(), gateway=gw)  # type: ignore[arg-type]
    svc._ready = True
    svc._enabled = True
    await svc._handle(_signal(SignalType.DUMP, symbol="AAAUSDT"))
    assert svc.enabled is True
    await svc._handle(_signal(SignalType.DUMP, symbol="BBBUSDT"))
    assert gw.place_calls == 2
    assert svc.enabled is True


@pytest.mark.asyncio
async def test_restart_without_auto_resume_reports_reason() -> None:
    gw = FakeGateway()
    repo = FakeRepo()
    repo.enabled = True
    notes: list[str] = []

    async def notify(text: str) -> None:
        notes.append(text)

    svc = TradingService(
        _settings(trading_auto_resume=False), repo, notify=notify, gateway=gw  # type: ignore[arg-type]
    )
    await svc.start()
    try:
        assert svc.enabled is False
        assert svc.disabled_reason == "restart_no_auto_resume"
        assert any("Торговый модуль запущен" in n and "TRADING_AUTO_RESUME" in n for n in notes)
        await svc._handle(_signal(SignalType.DUMP))
        skip = notes[-1]
        assert "Reason: disabled" in skip and "перезапущен" in skip
        assert gw.place_calls == 0
    finally:
        await svc.stop()


@pytest.mark.asyncio
async def test_restart_with_auto_resume_restores_on() -> None:
    repo = FakeRepo()
    repo.enabled = True
    svc = TradingService(
        _settings(trading_auto_resume=True), repo, gateway=FakeGateway()  # type: ignore[arg-type]
    )
    await svc.start()
    try:
        assert svc.enabled is True
        assert svc.disabled_reason == ""
    finally:
        await svc.stop()


@pytest.mark.asyncio
async def test_telegram_off_reason() -> None:
    svc = TradingService(_settings(), FakeRepo(), gateway=FakeGateway())  # type: ignore[arg-type]
    svc._ready = True
    assert await svc.set_enabled(True, chat_id=1) is None
    await svc.set_enabled(False, chat_id=1)
    assert svc.disabled_reason == "telegram_off"
    assert "кнопкой" in svc.describe_disabled_reason()
