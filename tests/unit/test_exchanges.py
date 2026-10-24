from datetime import UTC, datetime

from screener.application.notification_service import RecentSignals, recent_key
from screener.domain.enums import SignalType
from screener.domain.exchanges import (
    ALL_EXCHANGES,
    BINANCE,
    BITGET,
    BYBIT,
    exchanges_label,
    normalize_exchanges,
    toggle_exchange,
)
from tests.helpers import make_signal


def test_toggle_refuses_to_turn_off_the_last_exchange() -> None:
    only = frozenset({BYBIT})
    assert toggle_exchange(only, BYBIT) is None
    pair = frozenset({BYBIT, BINANCE})
    assert toggle_exchange(pair, BINANCE) == frozenset({BYBIT})
    assert toggle_exchange(frozenset({BYBIT}), BINANCE) == frozenset({BYBIT, BINANCE})
    assert toggle_exchange(frozenset(ALL_EXCHANGES), BITGET) == frozenset({BYBIT, BINANCE})


def test_toggle_keeps_one_running_exchange() -> None:
    running = frozenset({BYBIT, BITGET})
    assert toggle_exchange(frozenset({BYBIT, BINANCE}), BYBIT, running) is None
    assert toggle_exchange(frozenset(ALL_EXCHANGES), BYBIT, running) == frozenset(
        {BINANCE, BITGET}
    )


def test_normalize_and_label() -> None:
    assert normalize_exchanges(["bybit", "unknown", "binance"]) == frozenset({BYBIT, BINANCE})
    assert normalize_exchanges("nope") == frozenset(ALL_EXCHANGES)
    assert exchanges_label(frozenset({BYBIT, BINANCE})) == "ByBit, Binance"
    assert exchanges_label(frozenset({BINANCE})) == "Binance"
    assert exchanges_label(frozenset({BITGET})) == "Bitget"


def test_recent_key_keeps_exchanges_apart() -> None:
    recent = RecentSignals()
    now = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
    bybit = make_signal(SignalType.PUMP, 60, 5.0, symbol="BTCUSDT", exchange=BYBIT)
    binance = make_signal(SignalType.PUMP, 60, 5.0, symbol="BTCUSDT", exchange=BINANCE)
    recent.record(recent_key(bybit.symbol, bybit.exchange), now)
    recent.record(recent_key(binance.symbol, binance.exchange), now)
    recent.record(recent_key(binance.symbol, binance.exchange), now)
    assert recent.count(recent_key("BTCUSDT", BYBIT), now) == 1
    assert recent.count(recent_key("BTCUSDT", BINANCE), now) == 2
