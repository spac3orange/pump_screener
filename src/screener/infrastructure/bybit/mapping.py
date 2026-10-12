from __future__ import annotations

from typing import Any

from screener.config import TRADFI_SYMBOL_TYPES
from screener.domain.models import Instrument


def is_crypto_usdt_linear_perpetual(raw: dict[str, Any]) -> bool:
    if raw.get("contractType") != "LinearPerpetual":
        return False
    if raw.get("settleCoin") != "USDT":
        return False
    if raw.get("status") != "Trading":
        return False
    if raw.get("isPreListing") is True:
        return False
    symbol_type = raw.get("symbolType") or ""
    if symbol_type in TRADFI_SYMBOL_TYPES:
        return False
    return True


def map_instrument(raw: dict[str, Any]) -> Instrument:
    return Instrument(
        symbol=str(raw["symbol"]),
        contract_type=str(raw.get("contractType") or ""),
        status=str(raw.get("status") or ""),
        settle_coin=str(raw.get("settleCoin") or ""),
        base_coin=str(raw.get("baseCoin") or ""),
        quote_coin=str(raw.get("quoteCoin") or ""),
        symbol_type=(str(raw["symbolType"]) if raw.get("symbolType") else None),
        is_pre_listing=bool(raw.get("isPreListing") or False),
    )


def ticker_topic(symbol: str) -> str:
    return f"tickers.{symbol}"
