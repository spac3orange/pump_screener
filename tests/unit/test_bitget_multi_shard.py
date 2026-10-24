from types import SimpleNamespace

from screener.application.bitget_processor import BitgetTickerProcessor
from screener.application.metrics import FeedMetrics
from screener.application.price_history import PriceHistoryManager
from screener.workers.market_worker import MarketWorker


def _frame(symbol: str, ts_ms: int, price: str) -> dict:
    return {
        "action": "snapshot",
        "arg": {"instType": "usdt-futures", "topic": "ticker", "symbol": symbol},
        "data": [{"lastPrice": price, "turnover24h": "1000000", "volume24h": "10"}],
        "ts": ts_ms,
    }


def test_shard_connect_keeps_symbols_of_other_shards() -> None:
    stats = FeedMetrics()
    processor = BitgetTickerProcessor(stats)
    processor.set_universe(["AUSDT", "BUSDT"])
    feed = SimpleNamespace(
        exchange="bitget",
        primary=False,
        processor=processor,
        history=PriceHistoryManager(),
        stats=stats,
        commercial_engine=None,
    )
    state = SimpleNamespace(system=SimpleNamespace(market_queue_maxsize=100))
    worker = MarketWorker(state, feed=feed)  # type: ignore[arg-type]

    worker.on_ws_connect(["AUSDT"])
    worker.on_ws_connect(["BUSDT"])

    assert len(processor.apply_message(_frame("AUSDT", 1000, "1")) or []) == 1
    assert len(processor.apply_message(_frame("BUSDT", 1000, "2")) or []) == 1
