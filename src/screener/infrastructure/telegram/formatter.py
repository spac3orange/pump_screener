from __future__ import annotations

from screener.domain.models import SignalEvent


def bybit_trade_url(symbol: str) -> str:
    return f"https://www.bybit.com/trade/usdt/{symbol}"


def format_signal_message(signal: SignalEvent) -> str:
    interval_min = signal.interval_sec // 60
    vol_line = ""
    if signal.volume_ratio is not None:
        vol_line = f"Объём: x{signal.volume_ratio:.2f}\n"
    turnover_line = ""
    if signal.turnover_24h is not None:
        turnover_line = f"Turnover 24h: {signal.turnover_24h:,.0f} USDT\n"

    return (
        f"🚨 <b>{signal.signal_type.value}</b> {signal.symbol}\n"
        f"Изменение: {signal.change_pct:+.2f}% / {interval_min}m\n"
        f"Цена: {signal.price}\n"
        f"{vol_line}"
        f"{turnover_line}"
        f"Уровень: {signal.level}\n"
        f"Время: {signal.detected_at.strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
        f'<a href="{bybit_trade_url(signal.symbol)}">Открыть на Bybit</a>'
    )
