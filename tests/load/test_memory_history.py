"""Load-ish check: history for hundreds of symbols stays bounded."""

from screener.application.price_history import PriceHistoryManager
from screener.domain.enums import HistorySource


def test_history_memory_bound() -> None:
    h = PriceHistoryManager(maxlen=3700)
    symbols = [f"S{i}USDT" for i in range(800)]
    for sym in symbols:
        for t in range(100):
            h.add_tick(
                sym,
                ts_ms=t * 1000,
                last_price=1.0 + t * 0.001,
                turnover_24h=1000 + t,
                source=HistorySource.TICKER,
            )
    total_points = sum(h.point_count(s) for s in symbols)
    assert total_points == 800 * 100
    # ensure maxlen caps if we overfill one symbol
    for t in range(5000):
        h.add_tick(
            "BTCUSDT",
            ts_ms=t * 1000,
            last_price=1.0,
            turnover_24h=1.0,
            source=HistorySource.TICKER,
        )
    assert h.point_count("BTCUSDT") <= 3700
