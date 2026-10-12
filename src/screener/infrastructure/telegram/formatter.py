from __future__ import annotations

from screener.domain.enums import SignalType
from screener.domain.models import SignalEvent

_LABELS = {
    SignalType.PUMP: "Pump",
    SignalType.SHORT: "Short",
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


def format_signal_message(signal: SignalEvent, *, signals_24h: int = 1) -> str:
    minutes = signal.interval_sec // 60
    label = _LABELS.get(signal.signal_type, signal.signal_type.value)
    emoji = "🔻" if signal.signal_type == SignalType.DUMP else "🔴"
    change = f"{signal.change_pct:.2f}"
    prices = f"{format_price(signal.prev_price)}-{format_price(signal.price)}"
    return (
        f"ByBit – {minutes}M – {base_ticker(signal.symbol)}\n"
        f"{emoji} {label}: {change}% ({prices})\n"
        f"Signal 24h: {signals_24h}"
    )
