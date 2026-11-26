from screener.domain.enums import HistoryQuality, SignalType
from screener.domain.models import RuleMatch
from screener.infrastructure.telegram.formatter import (
    bybit_trade_url,
    format_price,
    format_signal_message,
    ticker_link,
)
from tests.helpers import T0, make_signal


def test_binance_ticker_link_and_message() -> None:
    assert ticker_link("BTCUSDT", "binance") == (
        '<a href="https://www.binance.com/en/futures/BTCUSDT">BTC</a>'
    )
    signal = make_signal(SignalType.PUMP, 180, 5.0, symbol="ETHUSDT", exchange="binance")
    text = format_signal_message(signal, signals_24h=3)
    assert text.startswith(
        '<a href="https://www.binance.com/">Binance</a> – 3M – '
        '<a href="https://www.binance.com/en/futures/ETHUSDT">ETH</a>'
    )
    assert "🔉 Signal 24h: 3" in text


def test_bitget_ticker_link_and_message() -> None:
    assert ticker_link("BTCUSDT", "bitget") == (
        '<a href="https://www.bitget.com/futures/usdt/BTCUSDT">BTC</a>'
    )
    signal = make_signal(SignalType.PUMP, 180, 5.0, symbol="ETHUSDT", exchange="bitget")
    text = format_signal_message(signal, signals_24h=2)
    assert text.startswith(
        '<a href="https://www.bitget.com/">Bitget</a> – 3M – '
        '<a href="https://www.bitget.com/futures/usdt/ETHUSDT">ETH</a>'
    )


BYBIT = '<a href="https://www.bybit.com/">ByBit</a>'


def test_bybit_url() -> None:
    assert bybit_trade_url("BTCUSDT") == "https://www.bybit.com/trade/usdt/BTCUSDT"


def test_ticker_link_is_clickable_html() -> None:
    assert ticker_link("BTRUSDT") == (
        '<a href="https://www.bybit.com/trade/usdt/BTRUSDT">BTR</a>'
    )


def test_format_price_keeps_small_decimals() -> None:
    assert format_price(0.0016619) == "0.0016619"
    assert format_price(0.0024808) == "0.0024808"


def test_pump_message_matches_screen_layout() -> None:
    signal = make_signal(
        SignalType.PUMP, 1800, 49.27, prev_price=0.0016619, price=0.0024808
    )
    text = format_signal_message(signal, signals_24h=1)
    assert text == (
        f'{BYBIT} – 30M – <a href="https://www.bybit.com/trade/usdt/RSRUSDT">RSR</a>\n'
        "🟢 Лонг: 49.27% (0.0016619-0.0024808)\n"
        "🔉 Signal 24h: 1"
    )


def test_dump_message_uses_triangle_and_minus() -> None:
    signal = make_signal(SignalType.DUMP, 900, -40.0, symbol="ETHUSDT", prev_price=100, price=60)
    text = format_signal_message(signal, signals_24h=2)
    assert text.startswith(
        f'{BYBIT} – 15M – <a href="https://www.bybit.com/trade/usdt/ETHUSDT">ETH</a>\n'
    )
    assert "🔻 Дамп: -40.00%" in text
    assert text.endswith("🔉 Signal 24h: 2")


def test_admin_alert_names_auto_trading_side() -> None:
    signal = make_signal(SignalType.PUMP, 720, 41.0)
    assert format_signal_message(signal).count("Автоторговля") == 0
    text = format_signal_message(signal, trade_action="SHORT")
    assert text.endswith("🔉 Signal 24h: 1\n🤖 Автоторговля: SHORT")


def test_signal_message_includes_profile_name() -> None:
    signal = make_signal(
        SignalType.PUMP, 180, 12.0, monitoring_profile_name="Night <fast>"
    )
    text = format_signal_message(signal, signals_24h=1)
    assert "📁 Профиль: Night &lt;fast&gt;" in text
    assert format_signal_message(make_signal(SignalType.PUMP, 180, 12.0)).count("Профиль") == 0


def test_short_message_uses_red_circle() -> None:
    signal = make_signal(SignalType.DROP, 1800, -10.09, prev_price=0.0114, price=0.01025)
    assert "🔴 Шорт: -10.09% (0.0114-0.01025)" in format_signal_message(signal)


def test_correlated_signal_shows_only_the_primary_rule() -> None:
    drop = RuleMatch(
        signal_type=SignalType.DROP,
        interval_sec=1800,
        threshold_pct=10.0,
        change_pct=-47.3,
        prev_price=0.55,
        price=0.2899,
        detected_ts_ms=T0,
        history_quality=HistoryQuality.LIVE,
    )
    signal = make_signal(
        SignalType.DUMP, 900, -42.1, symbol="XYZUSDT", prev_price=0.5, price=0.2895, extra=(drop,)
    )
    assert format_signal_message(signal, signals_24h=1) == (
        f'{BYBIT} – 15M – <a href="https://www.bybit.com/trade/usdt/XYZUSDT">XYZ</a>\n'
        "🔻 Дамп: -42.10% (0.5-0.2895)\n"
        "🔉 Signal 24h: 1"
    )
