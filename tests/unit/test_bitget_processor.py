from screener.application.bitget_processor import BitgetTickerProcessor
from screener.application.metrics import FeedMetrics
from screener.domain.enums import PriceEventKind


def _frame(symbol: str, ts_ms: int, price: str, *, turnover: str = "1000000") -> dict:
    return {
        "action": "snapshot",
        "arg": {"instType": "usdt-futures", "topic": "ticker", "symbol": symbol},
        "data": [{"lastPrice": price, "turnover24h": turnover, "volume24h": "10"}],
        "ts": ts_ms,
    }


def _proc(*symbols: str) -> tuple[BitgetTickerProcessor, FeedMetrics]:
    stats = FeedMetrics()
    proc = BitgetTickerProcessor(stats)
    proc.set_universe(symbols)
    return proc, stats


def test_first_ticker_is_snapshot_seed() -> None:
    proc, stats = _proc("BTCUSDT")
    events = proc.apply_message(_frame("BTCUSDT", 1000, "100"))
    assert events is not None and len(events) == 1
    assert events[0].kind is PriceEventKind.SNAPSHOT_SEED
    assert events[0].last_price == 100.0
    assert events[0].turnover_24h == 1_000_000.0
    assert stats.snapshot_seeds_total == 1
    assert stats.live_price_events_total == 0


def test_price_change_is_live() -> None:
    proc, stats = _proc("BTCUSDT")
    proc.apply_message(_frame("BTCUSDT", 1000, "100"))
    events = proc.apply_message(_frame("BTCUSDT", 2000, "101"))
    assert events is not None and events[0].kind is PriceEventKind.LIVE
    assert events[0].last_price == 101.0
    assert stats.live_price_events_total == 1


def test_same_price_is_enrichment() -> None:
    proc, stats = _proc("BTCUSDT")
    proc.apply_message(_frame("BTCUSDT", 1000, "100"))
    events = proc.apply_message(_frame("BTCUSDT", 2000, "100", turnover="2000000"))
    assert events == []
    assert stats.enrichment_updates_total == 1
    assert proc._states["BTCUSDT"].turnover_24h == 2_000_000.0


def test_out_of_order_envelope_ts_is_dropped() -> None:
    proc, stats = _proc("BTCUSDT")
    proc.apply_message(_frame("BTCUSDT", 2000, "100"))
    events = proc.apply_message(_frame("BTCUSDT", 1500, "90"))
    assert events == []
    assert stats.out_of_order_total == 1
    assert proc.last_price("BTCUSDT") == 100.0


def test_unknown_symbol_is_ignored() -> None:
    proc, stats = _proc("BTCUSDT")
    events = proc.apply_message(_frame("ETHUSDT", 1000, "1"))
    assert events is None or events == []
    assert stats.ticker_messages_total == 0


def test_resubscribe_reseeds_only_that_symbol() -> None:
    proc, _stats = _proc("BTCUSDT", "ETHUSDT")
    proc.apply_message(_frame("BTCUSDT", 1000, "100"))
    proc.apply_message(_frame("ETHUSDT", 1000, "10"))
    proc.on_resubscribe(["BTCUSDT"])
    btc = proc.apply_message(_frame("BTCUSDT", 3000, "100"))
    eth = proc.apply_message(_frame("ETHUSDT", 3000, "11"))
    assert btc is not None and btc[0].kind is PriceEventKind.SNAPSHOT_SEED
    assert eth is not None and eth[0].kind is PriceEventKind.LIVE
