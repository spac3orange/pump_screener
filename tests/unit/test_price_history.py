from screener.application.price_history import PriceHistoryManager
from screener.domain.enums import HistoryQuality, HistorySource

T0 = 1_700_000_000_000  # ms, second-aligned


def _sec(offset_sec: int) -> int:
    return T0 + offset_sec * 1000


def test_lookup_hits_exact_second() -> None:
    h = PriceHistoryManager()
    h.record("BTCUSDT", ts_ms=_sec(0), price=100.0)
    h.record("BTCUSDT", ts_ms=_sec(60), price=105.0)
    got = h.lookup("BTCUSDT", now_ms=_sec(60), interval_sec=60)
    assert got is not None
    assert got.price == 100.0
    assert got.quality is HistoryQuality.LIVE


def test_carry_forward_between_live_writes() -> None:
    h = PriceHistoryManager()
    h.record("ETHUSDT", ts_ms=_sec(0), price=100.0)
    h.record("ETHUSDT", ts_ms=_sec(600), price=120.0)
    # No trades between 0 and 600: last traded price stayed 100.
    got = h.lookup("ETHUSDT", now_ms=_sec(900), interval_sec=600)
    assert got is not None and got.price == 100.0
    assert h.point_count("ETHUSDT") == 601


def test_lookup_rejects_reference_before_history() -> None:
    h = PriceHistoryManager()
    h.record("ETHUSDT", ts_ms=_sec(100), price=100.0)
    h.record("ETHUSDT", ts_ms=_sec(130), price=110.0)
    assert h.lookup("ETHUSDT", now_ms=_sec(130), interval_sec=60) is None


def test_same_second_keeps_latest_price() -> None:
    h = PriceHistoryManager()
    h.record("SOLUSDT", ts_ms=_sec(1), price=1.0)
    h.record("SOLUSDT", ts_ms=_sec(1) + 400, price=1.1)
    assert h.point_count("SOLUSDT") == 1
    assert h.latest_price("SOLUSDT") == 1.1


def test_out_of_order_second_ignored() -> None:
    h = PriceHistoryManager()
    h.record("SOLUSDT", ts_ms=_sec(10), price=1.0)
    assert h.record("SOLUSDT", ts_ms=_sec(5), price=9.0) is False
    assert h.latest_price("SOLUSDT") == 1.0


def test_ring_capacity_bounds_history() -> None:
    h = PriceHistoryManager(capacity_sec=100)
    for s in range(0, 500):
        h.record("XUSDT", ts_ms=_sec(s), price=1.0 + s)
    first, last = h.span("XUSDT")  # type: ignore[misc]
    assert last - first + 1 == 100
    assert h.lookup("XUSDT", now_ms=_sec(499), interval_sec=60).price == 1.0 + 439  # type: ignore[union-attr]


def test_small_gap_within_tolerance_keeps_window_valid() -> None:
    h = PriceHistoryManager()
    for s in range(0, 100):
        h.record("AUSDT", ts_ms=_sec(s), price=100.0)
    h.mark_disconnected(["AUSDT"], at_ts_ms=_sec(100))
    h.record("AUSDT", ts_ms=_sec(103), price=101.0)  # 2s gap
    assert h.gaps("AUSDT") == [(_sec(101) // 1000, _sec(102) // 1000)]
    # 60s window tolerance is 3s -> 2s gap inside the window is fine.
    assert h.lookup("AUSDT", now_ms=_sec(150), interval_sec=60) is not None
    # Reference second inside the gap resolves to the pre-gap price within tolerance.
    assert h.lookup("AUSDT", now_ms=_sec(162), interval_sec=60).price == 100.0  # type: ignore[union-attr]


def test_outage_longer_than_capacity_asks_for_warmup() -> None:
    h = PriceHistoryManager(capacity_sec=120)
    h.record("CUSDT", ts_ms=_sec(0), price=100.0)
    h.mark_disconnected(["CUSDT"], at_ts_ms=_sec(0))
    h.record("CUSDT", ts_ms=_sec(500), price=100.0)
    assert h.take_needs_warmup() == ["CUSDT"]
    assert h.take_needs_warmup() == []


def test_large_gap_invalidates_only_crossing_windows() -> None:
    h = PriceHistoryManager()
    for s in range(0, 100):
        h.record("BUSDT", ts_ms=_sec(s), price=100.0)
    h.mark_disconnected(["BUSDT"], at_ts_ms=_sec(99))
    h.record("BUSDT", ts_ms=_sec(200), price=100.0)  # 100s gap
    for s in range(201, 300):
        h.record("BUSDT", ts_ms=_sec(s), price=100.0)
    # 60s window entirely after the gap: valid.
    assert h.lookup("BUSDT", now_ms=_sec(299), interval_sec=60) is not None
    # 240s window crosses the gap: invalid.
    assert h.lookup("BUSDT", now_ms=_sec(299), interval_sec=240) is None


def test_kline_warmup_fills_only_before_live_and_is_mixed() -> None:
    h = PriceHistoryManager()
    h.record("CUSDT", ts_ms=_sec(3600), price=200.0)
    closes = [(T0 // 1000 + 59 + 60 * i, 100.0 + i) for i in range(60)]
    written = h.backfill_klines("CUSDT", closes)
    assert written > 0
    got = h.lookup("CUSDT", now_ms=_sec(3600), interval_sec=1800)
    assert got is not None
    assert got.quality is HistoryQuality.MIXED
    # Live second untouched.
    assert h.latest_price("CUSDT") == 200.0


def test_kline_backfill_never_overwrites_ticker_points() -> None:
    h = PriceHistoryManager()
    for s in range(0, 120):
        h.record("DUSDT", ts_ms=_sec(s), price=50.0)
    h.backfill_klines("DUSDT", [(T0 // 1000, 1.0)], lo_sec=T0 // 1000, hi_sec=T0 // 1000 + 119)
    got = h.lookup("DUSDT", now_ms=_sec(119), interval_sec=60)
    assert got is not None and got.price == 50.0 and got.quality is HistoryQuality.LIVE


def test_backfill_source_enum_round_trip() -> None:
    h = PriceHistoryManager()
    h.record("EUSDT", ts_ms=_sec(0), price=1.0, source=HistorySource.KLINE)
    h.record("EUSDT", ts_ms=_sec(60), price=1.0)
    got = h.lookup("EUSDT", now_ms=_sec(60), interval_sec=60)
    assert got is not None and got.quality is HistoryQuality.MIXED


def test_ensure_capacity_grows_and_requests_warmup() -> None:
    h = PriceHistoryManager(capacity_sec=120)
    h.record("FUSDT", ts_ms=_sec(0), price=1.0)
    h.ensure_capacity(7200)
    assert h.capacity_sec == 7200
    assert h.take_needs_warmup() == ["FUSDT"]
    assert h.lookup("FUSDT", now_ms=_sec(60), interval_sec=60) is None
