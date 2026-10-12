from screener.application.price_history import PriceHistoryManager
from screener.domain.enums import HistorySource


def test_lookup_within_tolerance() -> None:
    h = PriceHistoryManager()
    # now = 100_000, interval 60s -> target 40_000
    h.seed_point("BTCUSDT", ts_ms=39_500, last_price=100.0, turnover_24h=1e6, source=HistorySource.TICKER)
    h.seed_point("BTCUSDT", ts_ms=100_000, last_price=105.0, turnover_24h=1.1e6, source=HistorySource.TICKER)
    got = h.lookup("BTCUSDT", now_ms=100_000, interval_sec=60)
    assert got is not None
    assert got.point.last_price == 100.0


def test_lookup_rejects_stale_gap() -> None:
    h = PriceHistoryManager()
    # target 40_000, point at 10_000 is far beyond 2s tolerance
    h.seed_point("ETHUSDT", ts_ms=10_000, last_price=100.0, turnover_24h=1e6, source=HistorySource.TICKER)
    h.seed_point("ETHUSDT", ts_ms=100_000, last_price=110.0, turnover_24h=1.1e6, source=HistorySource.TICKER)
    assert h.lookup("ETHUSDT", now_ms=100_000, interval_sec=60) is None


def test_downsample_one_hz() -> None:
    h = PriceHistoryManager()
    h.add_tick("SOLUSDT", ts_ms=1000, last_price=1.0, turnover_24h=10, source=HistorySource.TICKER)
    h.add_tick("SOLUSDT", ts_ms=1200, last_price=1.1, turnover_24h=11, source=HistorySource.TICKER)
    assert h.point_count("SOLUSDT") == 1
    assert h.latest("SOLUSDT").last_price == 1.1


def test_turnover_delta_requires_ticker_source() -> None:
    h = PriceHistoryManager()
    h.seed_point("XUSDT", ts_ms=40_000, last_price=1.0, turnover_24h=100, source=HistorySource.KLINE)
    h.seed_point("XUSDT", ts_ms=100_000, last_price=1.0, turnover_24h=200, source=HistorySource.TICKER)
    assert h.turnover_delta("XUSDT", now_ms=100_000, interval_sec=60) is None
