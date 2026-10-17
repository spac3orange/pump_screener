from screener.application.binance_processor import BinanceTickerProcessor
from screener.application.metrics import FeedMetrics
from screener.domain.enums import PriceEventKind


def _item(symbol: str, ts_ms: int, price: str, *, q: str = "1000000", v: str = "10") -> dict:
    return {"s": symbol, "E": ts_ms, "c": price, "q": q, "v": v}


def _frame(*items: dict) -> dict:
    return {"stream": "!ticker@arr", "data": list(items)}


def _proc(*symbols: str) -> tuple[BinanceTickerProcessor, FeedMetrics]:
    stats = FeedMetrics()
    proc = BinanceTickerProcessor(stats)
    proc.set_universe(symbols)
    return proc, stats


def test_first_ticker_is_snapshot_seed() -> None:
    proc, stats = _proc("BTCUSDT")
    events = proc.apply_message(_frame(_item("BTCUSDT", 1000, "100")))
    assert events is not None and len(events) == 1
    assert events[0].kind is PriceEventKind.SNAPSHOT_SEED
    assert events[0].last_price == 100.0
    assert stats.snapshot_seeds_total == 1
    assert stats.live_price_events_total == 0


def test_price_change_is_live() -> None:
    proc, stats = _proc("BTCUSDT")
    proc.apply_message(_frame(_item("BTCUSDT", 1000, "100")))
    events = proc.apply_message(_frame(_item("BTCUSDT", 2000, "101")))
    assert events is not None and events[0].kind is PriceEventKind.LIVE
    assert events[0].last_price == 101.0
    assert stats.live_price_events_total == 1


def test_same_price_is_enrichment() -> None:
    proc, stats = _proc("BTCUSDT")
    proc.apply_message(_frame(_item("BTCUSDT", 1000, "100")))
    events = proc.apply_message(_frame(_item("BTCUSDT", 2000, "100", q="2000000")))
    assert events == []
    assert stats.enrichment_updates_total == 1
    assert proc._states["BTCUSDT"].turnover_24h == 2_000_000.0


def test_out_of_order_event_time_is_dropped() -> None:
    proc, stats = _proc("BTCUSDT")
    proc.apply_message(_frame(_item("BTCUSDT", 2000, "100")))
    events = proc.apply_message(_frame(_item("BTCUSDT", 1500, "90")))
    assert events == []
    assert stats.out_of_order_total == 1
    assert proc.last_price("BTCUSDT") == 100.0


def test_unknown_symbol_is_ignored() -> None:
    proc, stats = _proc("BTCUSDT")
    events = proc.apply_message(_frame(_item("ETHUSDT", 1000, "1")))
    assert events is None or events == []
    assert stats.ticker_messages_total == 0


def test_every_item_in_the_frame_is_processed() -> None:
    proc, stats = _proc("BTCUSDT", "ETHUSDT", "SOLUSDT")
    events = proc.apply_message(
        _frame(
            _item("BTCUSDT", 1000, "100"),
            _item("ETHUSDT", 1001, "10"),
            _item("SOLUSDT", 1002, "20"),
        )
    )
    assert events is not None and [e.symbol for e in events] == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    assert all(e.kind is PriceEventKind.SNAPSHOT_SEED for e in events)
    live = proc.apply_message(
        _frame(
            _item("BTCUSDT", 2000, "101"),
            _item("ETHUSDT", 2001, "10"),
            _item("SOLUSDT", 2002, "21"),
        )
    )
    assert live is not None
    assert [e.symbol for e in live] == ["BTCUSDT", "SOLUSDT"]
    assert all(e.kind is PriceEventKind.LIVE for e in live)


def test_reconnect_reseeds_next_ticker() -> None:
    proc, _stats = _proc("BTCUSDT")
    proc.apply_message(_frame(_item("BTCUSDT", 1000, "100")))
    proc.on_resubscribe(["BTCUSDT"])
    events = proc.apply_message(_frame(_item("BTCUSDT", 3000, "100")))
    assert events is not None and events[0].kind is PriceEventKind.SNAPSHOT_SEED
