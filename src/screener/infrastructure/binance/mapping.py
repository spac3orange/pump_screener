from __future__ import annotations

from typing import Any

from screener.domain.exchanges import BINANCE
from screener.domain.models import Instrument


def is_crypto_usdt_perpetual(raw: dict[str, Any]) -> bool:
    """Crypto USDT-margined perpetuals only.

    ``TRADIFI_PERPETUAL`` (stocks, commodities), quarterly contracts, index
    contracts such as ``BTCDOMUSDT`` and pre-market contracts are excluded.
    """
    if raw.get("contractType") != "PERPETUAL":
        return False
    if raw.get("quoteAsset") != "USDT" or raw.get("marginAsset") != "USDT":
        return False
    if raw.get("status") != "TRADING":
        return False
    return raw.get("underlyingType") == "COIN"


def map_instrument(raw: dict[str, Any]) -> Instrument:
    return Instrument(
        symbol=str(raw["symbol"]),
        contract_type=str(raw.get("contractType") or ""),
        status=str(raw.get("status") or ""),
        settle_coin=str(raw.get("marginAsset") or ""),
        base_coin=str(raw.get("baseAsset") or ""),
        quote_coin=str(raw.get("quoteAsset") or ""),
        symbol_type=(str(raw["underlyingType"]) if raw.get("underlyingType") else None),
        is_pre_listing=False,
        exchange=BINANCE,
    )
