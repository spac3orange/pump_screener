from __future__ import annotations

from html import escape

from screener.domain.enums import SignalType
from screener.domain.exchanges import BYBIT, exchange_info
from screener.domain.models import RuleMatch

BYBIT_URL = "https://www.bybit.com/"
_LABELS = {
    SignalType.PUMP: "Лонг",
    SignalType.DROP: "Шорт",
    SignalType.DUMP: "Дамп",
}
_EMOJI = {
    SignalType.PUMP: "🟢",
    SignalType.DROP: "🔴",
    SignalType.DUMP: "🔻",
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


def _match_body(match: RuleMatch) -> str:
    prices = f"{format_price(match.prev_price)}-{format_price(match.price)}"
    return f"{match.change_pct:.2f}% ({prices})"


def format_signal_message(
    signal, *, signals_24h: int = 1, trade_action: str | None = None
) -> str:
    """One message shows one signal: the primary match; correlated windows are not listed.

    ``trade_action`` (admin only) names the side auto-trading takes, which may differ
    from the rule label: e.g. the Лонг rule can be configured to open a SHORT.
    """
    primary = signal.primary
    info = exchange_info(getattr(signal, "exchange", BYBIT))
    site = escape(info.site_url, quote=True)
    lines = [
        f'<a href="{site}">{escape(info.label)}</a> – {primary.interval_sec // 60}M – '
        f"{ticker_link(signal.symbol, info.code)}",
        f"{_EMOJI[primary.signal_type]} {_LABELS[primary.signal_type]}: {_match_body(primary)}",
        f"🔉 Signal 24h: {signals_24h}",
    ]
    profile_name = getattr(signal, "monitoring_profile_name", None)
    if profile_name:
        lines.append(f"📁 Профиль: {escape(str(profile_name))}")
    if trade_action:
        lines.append(f"🤖 Автоторговля: {escape(trade_action)}")
    return "\n".join(lines)
