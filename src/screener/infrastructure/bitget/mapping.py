"""Bitget UTA v3 instrument filter: crypto USDT perpetual only."""

from __future__ import annotations

from typing import Any

from screener.domain.exchanges import BITGET
from screener.domain.models import Instrument


def is_crypto_usdt_perpetual(raw: dict[str, Any]) -> bool:
    """USDT-margined crypto perpetuals that are online.

    ``symbolType`` other than ``crypto`` (stock, metal, commodity) and
    ``isRwa == YES`` are excluded. Classic expiry contracts are not in this
    category, but ``type`` is still required to be ``perpetual``.
    """
    if raw.get("type") != "perpetual":
        return False
    if raw.get("quoteCoin") != "USDT":
        return False
    if str(raw.get("status") or "").lower() != "online":
        return False
    if raw.get("symbolType") != "crypto":
        return False
    return str(raw.get("isRwa") or "NO").upper() != "YES"


def map_instrument(raw: dict[str, Any]) -> Instrument:
    return Instrument(
        symbol=str(raw["symbol"]),
        contract_type=str(raw.get("type") or ""),
        status=str(raw.get("status") or ""),
        settle_coin=str(raw.get("quoteCoin") or ""),
        base_coin=str(raw.get("baseCoin") or ""),
        quote_coin=str(raw.get("quoteCoin") or ""),
        symbol_type=(str(raw["symbolType"]) if raw.get("symbolType") else None),
        is_pre_listing=False,
        exchange=BITGET,
    )
