from screener.domain.exchanges import BITGET
from screener.infrastructure.bitget.mapping import is_crypto_usdt_perpetual, map_instrument


def _base(**overrides):
    raw = {
        "symbol": "BTCUSDT",
        "type": "perpetual",
        "status": "online",
        "baseCoin": "BTC",
        "quoteCoin": "USDT",
        "symbolType": "crypto",
        "isRwa": "NO",
    }
    raw.update(overrides)
    return raw


def test_accepts_crypto_usdt_perpetual() -> None:
    assert is_crypto_usdt_perpetual(_base())


def test_rejects_stock_metal_and_commodity() -> None:
    assert not is_crypto_usdt_perpetual(_base(symbol="AAPLUSDT", symbolType="stock"))
    assert not is_crypto_usdt_perpetual(_base(symbol="XAUUSDT", symbolType="metal"))
    assert not is_crypto_usdt_perpetual(_base(symbol="COFFEEUSDT", symbolType="commodity"))


def test_rejects_rwa() -> None:
    assert not is_crypto_usdt_perpetual(_base(isRwa="YES"))


def test_rejects_offline_and_non_usdt() -> None:
    assert not is_crypto_usdt_perpetual(_base(status="offline"))
    assert not is_crypto_usdt_perpetual(_base(quoteCoin="USDC"))
    assert not is_crypto_usdt_perpetual(_base(type="delivery"))


def test_map_instrument_sets_exchange() -> None:
    inst = map_instrument(_base())
    assert inst.symbol == "BTCUSDT"
    assert inst.exchange == BITGET
    assert inst.base_coin == "BTC"
    assert inst.quote_coin == "USDT"
    assert inst.symbol_type == "crypto"
