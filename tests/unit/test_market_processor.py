from screener.application.market_processor import MarketDataProcessor, normalize_klines
from screener.application.price_history import PriceHistoryManager
from screener.domain.enums import PriceEventKind


def _msg(msg_type: str, ts: int, cs: int, **data) -> dict:
    return {
        "topic": "tickers.BTCUSDT",
        "type": msg_type,
        "ts": ts,
        "cs": cs,
        "data": {"symbol": "BTCUSDT", **data},
    }


def test_snapshot_is_seed_and_delta_is_live() -> None:
    proc = MarketDataProcessor(PriceHistoryManager())
    seed = proc.apply_message(
        _msg("snapshot", 1_000_000, 1, lastPrice="100", turnover24h="1000", volume24h="10")
    )
    assert seed is not None
    assert seed.kind is PriceEventKind.SNAPSHOT_SEED
    assert seed.last_price == 100.0

    live = proc.apply_message(_msg("delta", 1_000_100, 2, lastPrice="101"))
    assert live is not None
    assert live.kind is PriceEventKind.LIVE
    assert live.last_price == 101.0
    assert live.turnover_24h == 1000.0
    assert live.exchange_ts_ms == 1_000_100


def test_delta_without_last_price_change_does_not_emit() -> None:
    proc = MarketDataProcessor(PriceHistoryManager())
    proc.apply_message(_msg("snapshot", 1_000_000, 1, lastPrice="100", turnover24h="1"))
    enrichment = _msg("delta", 1_000_100, 2, bid1Price="99.9", openInterest="5")
    assert proc.apply_message(enrichment) is None
    assert proc.apply_message(_msg("delta", 1_000_200, 3, lastPrice="100")) is None
    state = proc.get_state("BTCUSDT")
    assert state is not None
    assert state.bid1_price == 99.9
    assert state.open_interest == 5.0


def test_delta_before_snapshot_ignored() -> None:
    proc = MarketDataProcessor(PriceHistoryManager())
    assert proc.apply_message(_msg("delta", 1_000_000, 1, lastPrice="200")) is None


def test_resubscribe_requires_fresh_snapshot() -> None:
    proc = MarketDataProcessor(PriceHistoryManager())
    proc.apply_message(_msg("snapshot", 1_000_000, 10, lastPrice="100"))
    proc.on_resubscribe(["BTCUSDT"])
    # Delta arriving before the new snapshot must not mutate state.
    assert proc.apply_message(_msg("delta", 1_000_100, 1, lastPrice="150")) is None
    assert proc.get_state("BTCUSDT").last_price == 100.0  # type: ignore[union-attr]
    seed = proc.apply_message(_msg("snapshot", 1_000_200, 1, lastPrice="101"))
    assert seed is not None and seed.kind is PriceEventKind.SNAPSHOT_SEED


def test_old_cs_delta_ignored() -> None:
    proc = MarketDataProcessor(PriceHistoryManager())
    proc.apply_message(_msg("snapshot", 1_000_000, 5, lastPrice="100"))
    assert proc.apply_message(_msg("delta", 1_000_100, 4, lastPrice="90")) is None
    assert proc.get_state("BTCUSDT").last_price == 100.0  # type: ignore[union-attr]


def test_normalize_klines_already_ascending_binance() -> None:
    now_ms = 1_700_000_180_000
    rows = [
        ["1700000040000", "1", "1", "1", "1.0"],
        ["1700000100000", "1", "1", "1", "2.0"],
        ["1700000160000", "1", "1", "1", "3.0"],
    ]
    closes = normalize_klines(rows, now_ms=now_ms)
    assert closes == [(1_700_000_099, 1.0), (1_700_000_159, 2.0)]


def test_normalize_klines_ascending_and_drops_open_candle() -> None:
    now_ms = 1_700_000_180_000
    rows = [
        ["1700000160000", "1", "1", "1", "3.0"],  # still open at now_ms
        ["1700000100000", "1", "1", "1", "2.0"],
        ["1700000040000", "1", "1", "1", "1.0"],
    ]
    closes = normalize_klines(rows, now_ms=now_ms)
    assert closes == [(1_700_000_099, 1.0), (1_700_000_159, 2.0)]


def test_kline_warmup_does_not_touch_ticker_state() -> None:
    hist = PriceHistoryManager()
    proc = MarketDataProcessor(hist)
    proc.apply_message(_msg("snapshot", 1_700_000_600_000, 7, lastPrice="100"))
    before = proc.get_state("BTCUSDT")
    snapshot = (before.last_price, before.cs, before.has_snapshot)  # type: ignore[union-attr]
    hist.backfill_klines("BTCUSDT", [(1_700_000_059, 90.0), (1_700_000_119, 95.0)])
    after = proc.get_state("BTCUSDT")
    assert (after.last_price, after.cs, after.has_snapshot) == snapshot  # type: ignore[union-attr]
