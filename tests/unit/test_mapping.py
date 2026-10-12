from screener.infrastructure.bybit.mapping import is_crypto_usdt_linear_perpetual, map_instrument


def _base(**overrides):
    raw = {
        "symbol": "BTCUSDT",
        "contractType": "LinearPerpetual",
        "settleCoin": "USDT",
        "status": "Trading",
        "isPreListing": False,
        "baseCoin": "BTC",
        "quoteCoin": "USDT",
        "symbolType": "",
    }
    raw.update(overrides)
    return raw


def test_accepts_crypto_perp() -> None:
    assert is_crypto_usdt_linear_perpetual(_base())


def test_rejects_tradfi() -> None:
    assert not is_crypto_usdt_linear_perpetual(_base(symbolType="stock"))


def test_rejects_prelisting() -> None:
    assert not is_crypto_usdt_linear_perpetual(_base(isPreListing=True))


def test_rejects_futures() -> None:
    assert not is_crypto_usdt_linear_perpetual(_base(contractType="LinearFutures"))


def test_map_instrument() -> None:
    inst = map_instrument(_base())
    assert inst.symbol == "BTCUSDT"
    assert inst.settle_coin == "USDT"
