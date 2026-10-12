from datetime import UTC, datetime

from screener.domain.enums import SideHint, SignalType
from screener.domain.models import SignalEvent
from screener.infrastructure.telegram.formatter import bybit_trade_url, format_signal_message


def test_bybit_url() -> None:
    assert bybit_trade_url("BTCUSDT") == "https://www.bybit.com/trade/usdt/BTCUSDT"


def test_format_contains_fields() -> None:
    signal = SignalEvent(
        symbol="BTCUSDT",
        signal_type=SignalType.PUMP,
        side_hint=SideHint.LONG,
        interval_sec=600,
        change_pct=5.25,
        price=100.0,
        prev_price=95.0,
        detected_at=datetime(2026, 10, 10, 12, 0, tzinfo=UTC),
        rule_version="abc",
        volume_ratio=3.5,
        turnover_24h=1_000_000,
    )
    text = format_signal_message(signal)
    assert "PUMP" in text
    assert "BTCUSDT" in text
    assert "5.25" in text
    assert "Открыть на Bybit" in text
