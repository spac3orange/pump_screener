from datetime import UTC, datetime

from screener.domain.enums import SideHint, SignalType
from screener.domain.models import SignalEvent
from screener.infrastructure.telegram.formatter import (
    bybit_trade_url,
    format_price,
    format_signal_message,
)


def test_bybit_url() -> None:
    assert bybit_trade_url("BTCUSDT") == "https://www.bybit.com/trade/usdt/BTCUSDT"


def test_format_price_keeps_small_decimals() -> None:
    assert format_price(0.0016619) == "0.0016619"
    assert format_price(0.0024808) == "0.0024808"


def test_pump_message_matches_screen_layout() -> None:
    signal = SignalEvent(
        symbol="RSRUSDT",
        signal_type=SignalType.PUMP,
        side_hint=SideHint.LONG,
        interval_sec=1800,
        change_pct=49.27,
        price=0.0024808,
        prev_price=0.0016619,
        detected_at=datetime(2026, 10, 12, 8, 4, tzinfo=UTC),
        rule_version="abc",
    )
    text = format_signal_message(signal, signals_24h=1)
    assert text == (
        "ByBit – 30M – RSR\n"
        "🔴 Pump: 49.27% (0.0016619-0.0024808)\n"
        "Signal 24h: 1"
    )


def test_dump_message_uses_triangle_and_minus() -> None:
    signal = SignalEvent(
        symbol="ETHUSDT",
        signal_type=SignalType.DUMP,
        side_hint=SideHint.SHORT,
        interval_sec=900,
        change_pct=-40.0,
        price=60.0,
        prev_price=100.0,
        detected_at=datetime(2026, 10, 12, 8, 4, tzinfo=UTC),
        rule_version="abc",
    )
    text = format_signal_message(signal, signals_24h=2)
    assert text.startswith("ByBit – 15M – ETH\n")
    assert "🔻 Dump: -40.00%" in text
    assert text.endswith("Signal 24h: 2")
