from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from screener.domain.trade_lifecycle import (
    PositionCloseReason,
    TradeClosedEvent,
    TradeNotificationType,
)
from screener.infrastructure.telegram.trade_lifecycle_format import (
    format_duration,
    format_money,
    format_trade_closed,
)

D = Decimal


def event(**kw) -> TradeClosedEvent:
    opened = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
    base = dict(
        trade_id=uuid4(),
        signal_id=uuid4(),
        symbol="EPICUSDT",
        side="Buy",
        mode="mainnet",
        close_reason=PositionCloseReason.TAKE_PROFIT,
        reason_detail="сработал Take Profit",
        entry_price=D("0.3102"),
        exit_price=D("0.3291"),
        qty=D("100"),
        closed_qty=D("100"),
        opened_at=opened,
        closed_at=opened + timedelta(minutes=17, seconds=42),
        realized_pnl=D("0.41"),
        fees=D("0.02"),
        funding=D("0"),
        net_pnl=D("0.39"),
        bybit_closed_pnl=None,
        balance_after=D("10.73"),
        close_order_id="o",
        close_execution_id="x",
    )
    base.update(kw)
    return TradeClosedEvent(**base)


def test_take_profit_message_matches_spec() -> None:
    assert format_trade_closed(event()) == (
        "✅ <b>EPICUSDT · TAKE PROFIT</b>\n"
        "\n"
        "LONG закрыт\n"
        "\n"
        "Entry: <code>0.3102</code>\n"
        "Exit: <code>0.3291</code>\n"
        "\n"
        "Price PnL: <b>+6.09%</b>\n"
        "Realized PnL: <b>+$0.41</b>\n"
        "Fees: $0.02\n"
        "Net PnL: <b>+$0.39</b>\n"
        "\n"
        "Время сделки: 17м 42с\n"
        "Баланс: <b>$10.73</b>"
    )


def test_stop_loss_negative_values() -> None:
    text = format_trade_closed(
        event(
            close_reason=PositionCloseReason.STOP_LOSS,
            exit_price=D("0.3009"),
            realized_pnl=D("-0.21"),
            net_pnl=D("-0.23"),
        )
    )
    assert text.startswith("🛑 <b>EPICUSDT · STOP LOSS</b>")
    assert "Price PnL: <b>-3.00%</b>" in text
    assert "Net PnL: <b>-$0.23</b>" in text


def test_unknown_shows_reason_and_omits_missing_values() -> None:
    text = format_trade_closed(
        event(
            close_reason=PositionCloseReason.UNKNOWN,
            reason_detail="нет данных об исполнении закрытия",
            exit_price=None,
            realized_pnl=None,
            fees=None,
            funding=None,
            net_pnl=None,
            balance_after=None,
        )
    )
    assert text.startswith("⚪ <b>EPICUSDT · POSITION CLOSED</b>")
    assert "Причина: UNKNOWN — нет данных об исполнении закрытия" in text
    assert "Exit" not in text and "PnL" not in text and "Fees" not in text
    assert "Баланс: недоступен" in text


def test_external_and_liquidation_headers() -> None:
    external = format_trade_closed(event(close_reason=PositionCloseReason.EXTERNAL_ORDER))
    assert "⚪ <b>EPICUSDT · POSITION CLOSED</b>" in external
    assert "Причина: внешнее закрытие" in external
    liq = format_trade_closed(event(close_reason=PositionCloseReason.LIQUIDATION))
    assert liq.startswith("⚠️ <b>EPICUSDT · LIQUIDATION</b>")


def test_html_is_escaped() -> None:
    text = format_trade_closed(
        event(close_reason=PositionCloseReason.EXTERNAL_ORDER, reason_detail="<b>x</b> & y")
    )
    assert "&lt;b&gt;x&lt;/b&gt; &amp; y" in text


def test_short_recovered_demo_with_funding_and_bybit_fallback() -> None:
    text = format_trade_closed(
        event(
            side="Sell",
            mode="demo",
            exit_price=D("0.3009"),
            funding=D("0.004"),
            net_pnl=None,
            bybit_closed_pnl=D("0.37"),
            recovered=True,
        )
    )
    assert "SHORT закрыт" in text
    assert "Price PnL: <b>+3.00%</b>" in text
    assert "Funding: -$0.004" in text
    assert "Net PnL (Bybit): <b>+$0.37</b>" in text
    assert "Восстановлено после перезапуска" in text
    assert text.endswith("<i>DEMO</i>")


def test_notification_types() -> None:
    assert event().notification_type is TradeNotificationType.TRADE_CLOSED_TP
    assert (
        event(close_reason=PositionCloseReason.MANUAL).notification_type
        is TradeNotificationType.TRADE_CLOSED_EXTERNAL
    )
    assert (
        event(close_reason=PositionCloseReason.LIQUIDATION).notification_type
        is TradeNotificationType.TRADE_LIQUIDATED
    )


def test_helpers() -> None:
    assert format_duration(timedelta(seconds=42)) == "42с"
    assert format_duration(timedelta(hours=2, minutes=5)) == "2ч 05м"
    assert format_duration(timedelta(days=1, hours=3)) == "1д 3ч 00м"
    assert format_money(D("1234.5")) == "+$1,234.50"
    assert format_money(D("-0.0042")) == "-$0.0042"
    assert format_money(D("0"), signed=False) == "$0.00"
