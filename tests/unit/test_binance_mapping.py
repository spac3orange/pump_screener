from screener.infrastructure.binance.mapping import is_crypto_usdt_perpetual, map_instrument


def _base(**overrides):
    raw = {
        "symbol": "BTCUSDT",
        "pair": "BTCUSDT",
        "contractType": "PERPETUAL",
        "status": "TRADING",
        "baseAsset": "BTC",
        "quoteAsset": "USDT",
        "marginAsset": "USDT",
        "underlyingType": "COIN",
    }
    raw.update(overrides)
    return raw


def test_accepts_crypto_usdt_perpetual() -> None:
    assert is_crypto_usdt_perpetual(_base())


def test_rejects_tradifi() -> None:
    assert not is_crypto_usdt_perpetual(_base(contractType="TRADIFI_PERPETUAL", symbol="AAPLUSDT"))


def test_rejects_quarterly() -> None:
    assert not is_crypto_usdt_perpetual(
        _base(contractType="CURRENT_QUARTER", symbol="BTCUSDT_260327")
    )


def test_rejects_index() -> None:
    assert not is_crypto_usdt_perpetual(_base(symbol="BTCDOMUSDT", underlyingType="INDEX"))


def test_rejects_premarket() -> None:
    assert not is_crypto_usdt_perpetual(_base(underlyingType="PREMARKET"))


def test_rejects_non_usdt_margin() -> None:
    assert not is_crypto_usdt_perpetual(_base(marginAsset="USDC"))


def test_rejects_non_trading() -> None:
    assert not is_crypto_usdt_perpetual(_base(status="PENDING_TRADING"))


def test_filter_on_real_exchangeinfo_shape() -> None:
    symbols = [
        _base(),
        _base(symbol="ETHUSDT"),
        _base(symbol="AAPLUSDT", contractType="TRADIFI_PERPETUAL", underlyingType="EQUITY"),
        _base(symbol="BTCUSDT_260327", contractType="CURRENT_QUARTER"),
        _base(symbol="BTCDOMUSDT", underlyingType="INDEX"),
        _base(symbol="XYZUSDT", underlyingType="PREMARKET"),
        _base(symbol="XAGUSDT", underlyingType="COMMODITY"),
        _base(symbol="EURUSDT", underlyingType="FX"),
        _base(symbol="OLDUSDT", status="SETTLING"),
    ]
    kept = [s["symbol"] for s in symbols if is_crypto_usdt_perpetual(s)]
    assert kept == ["BTCUSDT", "ETHUSDT"]


def test_map_instrument() -> None:
    inst = map_instrument(_base())
    assert inst.symbol == "BTCUSDT"
    assert inst.settle_coin == "USDT"
    assert inst.exchange == "binance"
    assert inst.symbol_type == "COIN"
