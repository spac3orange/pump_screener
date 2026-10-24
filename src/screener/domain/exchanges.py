"""Exchanges the screener monitors. Only Bybit is ever traded."""

from __future__ import annotations

from dataclasses import dataclass

BYBIT = "bybit"
BINANCE = "binance"
BITGET = "bitget"
ALL_EXCHANGES: tuple[str, ...] = (BYBIT, BINANCE, BITGET)
TRADING_EXCHANGE = BYBIT


@dataclass(frozen=True, slots=True)
class ExchangeInfo:
    code: str
    label: str
    site_url: str
    trade_url_template: str

    def trade_url(self, symbol: str) -> str:
        return self.trade_url_template.format(symbol=symbol)


EXCHANGES: dict[str, ExchangeInfo] = {
    BYBIT: ExchangeInfo(
        code=BYBIT,
        label="ByBit",
        site_url="https://www.bybit.com/",
        trade_url_template="https://www.bybit.com/trade/usdt/{symbol}",
    ),
    BINANCE: ExchangeInfo(
        code=BINANCE,
        label="Binance",
        site_url="https://www.binance.com/",
        trade_url_template="https://www.binance.com/en/futures/{symbol}",
    ),
    BITGET: ExchangeInfo(
        code=BITGET,
        label="Bitget",
        site_url="https://www.bitget.com/",
        trade_url_template="https://www.bitget.com/futures/usdt/{symbol}",
    ),
}


def exchange_info(code: str) -> ExchangeInfo:
    return EXCHANGES.get(code, EXCHANGES[BYBIT])


def normalize_exchanges(values: object) -> frozenset[str]:
    """Known exchange codes from stored data; unknown values are ignored."""
    if not isinstance(values, (list, tuple, set, frozenset)):
        return frozenset(ALL_EXCHANGES)
    return frozenset(v for v in values if v in EXCHANGES)


def toggle_exchange(
    current: frozenset[str], code: str, available: frozenset[str] | None = None
) -> frozenset[str] | None:
    """Return the new set, or ``None`` if this would turn off the last exchange.

    ``available`` = exchanges whose feed runs; an enabled but stopped feed sends nothing,
    so it does not count as the remaining one.
    """
    if code not in EXCHANGES:
        return current
    if code in current:
        nxt = current - {code}
        alive = nxt if available is None else nxt & available
        return None if not alive else nxt
    return current | {code}


def exchanges_label(values: frozenset[str]) -> str:
    return ", ".join(EXCHANGES[c].label for c in ALL_EXCHANGES if c in values) or "—"
