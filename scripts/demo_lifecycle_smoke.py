"""Bybit DEMO smoke: open via TradingService → close by TP / SL / API order → lifecycle event.

Refuses to run unless BYBIT_TRADING_MODE=demo. Uses a throwaway PostgreSQL
(SMOKE_DATABASE_URL, database name must contain "test"); migrates it to head.

    $env:PYTHONPATH="src;."; $env:SMOKE_DATABASE_URL="postgresql+asyncpg://.../screener_test"
    python scripts/demo_lifecycle_smoke.py --symbol ETHUSDT --runs tp sl external

A tight bracket (TP/SL ±BRACKET_PCT) makes the exchange close the position within minutes;
whether a run ends on TP or SL is up to the market, so "tp"/"sl" runs repeat until the wanted
outcome is observed (bounded by --max-attempts). "external" closes with a reduce-only API
market order (classified EXTERNAL_ORDER). A Bybit UI "Close" (MANUAL) needs a human.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import text

from bybit_client import BybitGateway, OrderSide, create_bybit_http
from screener.application.trading.service import TradingService
from screener.config import TradingSettings
from screener.domain.enums import Direction, HistoryQuality, SignalType
from screener.domain.models import MarketSignal, RuleMatch
from screener.domain.trade_lifecycle import TradeClosedEvent
from screener.infrastructure.bybit.lifecycle_runtime import TradeLifecycleRuntime
from screener.infrastructure.db.session import create_engine, create_session_factory
from screener.infrastructure.db.trading_repo import TradingRepository
from screener.infrastructure.telegram.trade_lifecycle_format import format_trade_closed

URL = os.getenv("SMOKE_DATABASE_URL", "")


def _alembic_head() -> None:
    from alembic.config import Config

    from alembic import command

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", URL)
    command.upgrade(cfg, "head")


def _signal(symbol: str, price: float) -> MarketSignal:
    match = RuleMatch(
        signal_type=SignalType.DUMP,
        interval_sec=300,
        threshold_pct=10.0,
        change_pct=-10.0,
        prev_price=price * 1.1,
        price=price,
        detected_ts_ms=int(time.time() * 1000),
        history_quality=HistoryQuality.LIVE,
    )
    return MarketSignal(
        symbol=symbol,
        direction=Direction.DOWN,
        primary=match,
        matches=(match,),
        detected_at=datetime.now(UTC),
        rule_version="demo-smoke",
    )


async def _wait_row(engine, signal_id, statuses, timeout: float):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT * FROM trading_executions WHERE signal_id = :s"),
                    {"s": signal_id},
                )
            ).mappings().first()
        if row is not None and row["status"] in statuses:
            return row
        await asyncio.sleep(0.5)
    return None


def _jsonable(row) -> dict:
    out = {}
    for key, value in dict(row).items():
        if key in {"error_message"}:
            continue
        out[key] = str(value) if value is not None else None
    return out


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="ETHUSDT")
    parser.add_argument("--runs", nargs="+", default=["tp", "sl", "external"])
    parser.add_argument("--bracket-pct", type=float, default=0.08)
    parser.add_argument("--margin-pct", type=float, default=0.05)
    parser.add_argument("--max-attempts", type=int, default=6)
    parser.add_argument("--close-timeout", type=float, default=600)
    args = parser.parse_args()

    if "test" not in URL.rsplit("/", 1)[-1]:
        print("SMOKE_DATABASE_URL to a *test* database is required", file=sys.stderr)
        return 2
    base = TradingSettings()
    if base.bybit_trading_mode != "demo":
        print("Refusing: BYBIT_TRADING_MODE must be demo", file=sys.stderr)
        return 2
    settings = base.model_copy(
        update={
            "dump_action": "LONG",
            "trade_margin_pct": args.margin_pct,
            "max_open_positions": 10,
            "take_profit_pct": args.bracket_pct,
            "stop_loss_pct": args.bracket_pct,
            "tp_trigger_by": "LastPrice",
            "sl_trigger_by": "LastPrice",
            "trading_notify_skips": True,
            "lifecycle_pnl_wait_sec": 5.0,
        }
    )

    await asyncio.to_thread(_alembic_head)
    engine = create_engine(URL)
    repo = TradingRepository(create_session_factory(engine))
    gateway = BybitGateway(
        create_bybit_http(
            api_key=settings.bybit_api_key, api_secret=settings.bybit_api_secret, demo=True
        ),
        demo=True,
        api_key=settings.bybit_api_key,
        api_secret=settings.bybit_api_secret,
        start_pybit_ws=False,
    )
    runtime = TradeLifecycleRuntime(settings, gateway, repo)
    trading = TradingService(settings, repo, gateway=gateway)
    trading.set_position_listener(runtime.service.notice_trade_opened)

    ws_log: list[dict] = []
    closed: asyncio.Queue[TradeClosedEvent] = asyncio.Queue()
    original = runtime.stream._on_message  # noqa: SLF001

    async def tap(message: dict) -> None:
        topic = message.get("topic", "")
        for row in message.get("data") or []:
            if row.get("symbol") != args.symbol:
                continue
            keep = (
                "orderId", "orderStatus", "stopOrderType", "createType", "cancelType",
                "execType", "execId", "side", "execQty", "closedSize", "execPrice",
                "execPnl", "execFee", "size", "positionStatus", "triggerPrice",
            )
            ws_log.append({"t": round(time.time(), 3), "topic": topic,
                           **{k: row[k] for k in keep if row.get(k) not in (None, "")}})
        await original(message)

    runtime.stream._on_message = tap  # noqa: SLF001

    async def notify_close(event: TradeClosedEvent) -> bool:
        await closed.put(event)
        return True

    async def notify_text(message: str) -> None:
        print(f"\n[telegram:text]\n{message}\n")

    runtime.service.set_notify(notify_close, notify_text)
    trading.set_notify(notify_text)

    await trading.start()
    if not trading.ready:
        print(f"Trading not ready: {trading.ready_reason}", file=sys.stderr)
        return 1
    if (err := await trading.set_enabled(True)) is not None:
        print(f"Cannot enable trading: {err}", file=sys.stderr)
        return 1
    await runtime.start()
    for _ in range(40):
        if runtime.stream.connected:
            break
        await asyncio.sleep(0.5)
    print(f"private WS connected={runtime.stream.connected}")

    results = []
    try:
        for wanted in args.runs:
            for attempt in range(1, args.max_attempts + 1):
                position = await gateway.get_position(args.symbol)
                if position.size > 0:
                    print(f"{args.symbol} already has a position; aborting", file=sys.stderr)
                    return 1
                ws_log.clear()
                ticker = await asyncio.to_thread(
                    gateway._http.get_tickers, category="linear", symbol=args.symbol  # noqa: SLF001
                )
                price = float(ticker["result"]["list"][0]["lastPrice"])
                signal = _signal(args.symbol, price)
                print(f"\n=== run {wanted} attempt {attempt}: open LONG {args.symbol} ~{price}")
                trading.enqueue(signal)
                row = await _wait_row(
                    engine,
                    signal.id,
                    {"FILLED", "OPEN", "PROTECTIVE_FAILED", "FAILED", "REJECTED", "SKIPPED"},
                    60,
                )
                if row is None or row["status"] in {"FAILED", "REJECTED", "SKIPPED"}:
                    print(f"entry did not fill: {row and _jsonable(row)}", file=sys.stderr)
                    return 1
                print(f"entry: qty={row['filled_qty']} avg={row['avg_fill_price']}")

                if wanted == "external":
                    await asyncio.sleep(3)
                    await asyncio.to_thread(
                        gateway._http.place_order,  # noqa: SLF001
                        category="linear",
                        symbol=args.symbol,
                        side=OrderSide.SELL.value,
                        orderType="Market",
                        qty=str(row["filled_qty"]),
                        reduceOnly=True,
                        orderLinkId=f"smoke-ext-{uuid4().hex[:12]}",
                    )
                try:
                    event = await asyncio.wait_for(closed.get(), args.close_timeout)
                except TimeoutError:
                    print("no close event in time; closing position", file=sys.stderr)
                    return 1
                await asyncio.sleep(0.5)
                async with engine.connect() as conn:
                    db_row = (
                        await conn.execute(
                            text("SELECT * FROM trading_executions WHERE id = :i"),
                            {"i": event.trade_id},
                        )
                    ).mappings().first()
                print("--- WS events (this symbol):")
                for item in ws_log:
                    print("   ", json.dumps(item, ensure_ascii=False))
                print(f"--- classification: {event.close_reason.value} ({event.reason_detail})")
                print("--- DB row:")
                print(json.dumps(_jsonable(db_row), ensure_ascii=False, indent=1))
                print("--- Telegram message:")
                print(format_trade_closed(event))
                results.append((wanted, event.close_reason.value))
                expected = {"tp": "TAKE_PROFIT", "sl": "STOP_LOSS", "external": "EXTERNAL_ORDER"}
                if event.close_reason.value == expected[wanted]:
                    break
                print(f"(wanted {expected[wanted]}, got {event.close_reason.value}; retrying)")
                await asyncio.sleep(2)
    finally:
        position = await gateway.get_position(args.symbol)
        if position.size > 0 and position.side == "Buy":
            await asyncio.to_thread(
                gateway._http.place_order,  # noqa: SLF001
                category="linear",
                symbol=args.symbol,
                side="Sell",
                orderType="Market",
                qty=str(position.size),
                reduceOnly=True,
            )
            print("cleanup: closed leftover smoke position")
        await runtime.stop()
        await trading.stop()
        await engine.dispose()
    print("\nRESULTS:", results)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
