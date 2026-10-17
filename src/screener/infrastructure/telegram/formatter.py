from __future__ import annotations

from html import escape

from screener.domain.enums import SignalType
from screener.domain.models import MarketSignal, RuleMatch

BYBIT_URL = "https://www.bybit.com/"
_LABELS = {
    SignalType.PUMP: "Pump",
    SignalType.DROP: "Drop",
    SignalType.DUMP: "Dump",
}


def bybit_trade_url(symbol: str) -> str:
    return f"https://www.bybit.com/trade/usdt/{symbol}"


def format_price(value: float) -> str:
    text = f"{value:.10f}".rstrip("0").rstrip(".")
    return text or "0"


def base_ticker(symbol: str) -> str:
    if symbol.endswith("USDT"):
        return symbol[: -len("USDT")]
    return symbol


def ticker_link(symbol: str) -> str:
    """HTML link to the Bybit USDT perpetual page; label is the base ticker."""
    label = escape(base_ticker(symbol))
    href = escape(bybit_trade_url(symbol), quote=True)
    return f'<a href="{href}">{label}</a>'


def _emoji(signal_type: SignalType) -> str:
    return "🔻" if signal_type == SignalType.DUMP else "🔴"


def _match_body(match: RuleMatch) -> str:
    prices = f"{format_price(match.prev_price)}-{format_price(match.price)}"
    return f"{match.change_pct:.2f}% ({prices})"


def format_signal_message(signal: MarketSignal, *, signals_24h: int = 1) -> str:
    """One message shows one signal: the primary match; correlated windows are not listed."""
    primary = signal.primary
    return "\n".join(
        [
            f'<a href="{BYBIT_URL}">ByBit</a> – {primary.interval_sec // 60}M – '
            f"{ticker_link(signal.symbol)}",
            f"{_emoji(primary.signal_type)} {_LABELS[primary.signal_type]}: {_match_body(primary)}",
            f"🔉 Signal 24h: {signals_24h}",
        ]
    )
