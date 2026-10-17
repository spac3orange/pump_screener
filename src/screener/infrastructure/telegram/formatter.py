from __future__ import annotations

from html import escape

from screener.domain.enums import SignalType
from screener.domain.exchanges import BYBIT, exchange_info
from screener.domain.models import RuleMatch

BYBIT_URL = "https://www.bybit.com/"
_LABELS = {
    SignalType.PUMP: "Pump",
    SignalType.DROP: "Drop",
    SignalType.DUMP: "Dump",
}


def bybit_trade_url(symbol: str) -> str:
    return exchange_info(BYBIT).trade_url(symbol)


def format_price(value: float) -> str:
    text = f"{value:.10f}".rstrip("0").rstrip(".")
    return text or "0"


def base_ticker(symbol: str) -> str:
    if symbol.endswith("USDT"):
        return symbol[: -len("USDT")]
    return symbol


def ticker_link(symbol: str, exchange: str = BYBIT) -> str:
    """HTML link to the exchange's perpetual page; label is the base ticker."""
    label = escape(base_ticker(symbol))
    href = escape(exchange_info(exchange).trade_url(symbol), quote=True)
    return f'<a href="{href}">{label}</a>'


def _emoji(signal_type: SignalType) -> str:
    return "🔻" if signal_type == SignalType.DUMP else "🔴"


def _match_body(match: RuleMatch) -> str:
    prices = f"{format_price(match.prev_price)}-{format_price(match.price)}"
    return f"{match.change_pct:.2f}% ({prices})"


def format_signal_message(signal, *, signals_24h: int = 1) -> str:
    """One message shows one signal: the primary match; correlated windows are not listed."""
    primary = signal.primary
    info = exchange_info(getattr(signal, "exchange", BYBIT))
    site = escape(info.site_url, quote=True)
    return "\n".join(
        [
            f'<a href="{site}">{escape(info.label)}</a> – {primary.interval_sec // 60}M – '
            f"{ticker_link(signal.symbol, info.code)}",
            f"{_emoji(primary.signal_type)} {_LABELS[primary.signal_type]}: {_match_body(primary)}",
            f"🔉 Signal 24h: {signals_24h}",
        ]
    )
