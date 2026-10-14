"""Telegram HTML for ``TradeClosedEvent`` (separate from the market-signal formatter).

Only values actually obtained from Bybit are printed; missing ones are omitted.
"""

from __future__ import annotations

import html
from datetime import timedelta
from decimal import Decimal

from screener.domain.trade_lifecycle import PositionCloseReason, TradeClosedEvent

_HEADER = {
    PositionCloseReason.TAKE_PROFIT: ("✅", "TAKE PROFIT"),
    PositionCloseReason.STOP_LOSS: ("🛑", "STOP LOSS"),
    PositionCloseReason.MANUAL: ("⚪", "POSITION CLOSED"),
    PositionCloseReason.EXTERNAL_ORDER: ("⚪", "POSITION CLOSED"),
    PositionCloseReason.LIQUIDATION: ("⚠️", "LIQUIDATION"),
    PositionCloseReason.ADL: ("⚠️", "AUTO-DELEVERAGING (ADL)"),
    PositionCloseReason.UNKNOWN: ("⚪", "POSITION CLOSED"),
}
_REASON_LINE = {
    PositionCloseReason.MANUAL: "ручное закрытие на Bybit",
    PositionCloseReason.EXTERNAL_ORDER: "внешнее закрытие",
    PositionCloseReason.LIQUIDATION: "ликвидация",
    PositionCloseReason.ADL: "принудительное закрытие биржей (ADL)",
    PositionCloseReason.UNKNOWN: "UNKNOWN",
}


def format_price(value: Decimal) -> str:
    return format(value.normalize(), "f")


def format_money(value: Decimal, *, signed: bool = True) -> str:
    places = Decimal("0.01") if abs(value) >= Decimal("0.01") or value == 0 else Decimal("0.0001")
    amount = f"{abs(value).quantize(places):,}"
    if value < 0:
        return f"-${amount}"
    return f"+${amount}" if signed else f"${amount}"


def format_pct(value: Decimal) -> str:
    rounded = abs(value).quantize(Decimal("0.01"))
    if rounded == 0:
        return "0.00%"
    return f"{'+' if value > 0 else '-'}{rounded}%"


def format_duration(delta: timedelta) -> str:
    total = int(delta.total_seconds())
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, seconds = divmod(rest, 60)
    if days:
        return f"{days}д {hours}ч {minutes:02d}м"
    if hours:
        return f"{hours}ч {minutes:02d}м"
    if minutes:
        return f"{minutes}м {seconds:02d}с"
    return f"{seconds}с"


def format_trade_closed(event: TradeClosedEvent) -> str:
    emoji, title = _HEADER[event.close_reason]
    lines = [f"{emoji} <b>{html.escape(event.symbol)} · {title}</b>", ""]
    lines.append(f"{event.direction} закрыт")
    reason = _REASON_LINE.get(event.close_reason)
    if reason is not None:
        detail = event.reason_detail if event.reason_detail != reason else ""
        text = f"{reason} — {detail}" if detail else reason
        lines.append(f"Причина: {html.escape(text)}")

    prices = []
    if event.entry_price is not None:
        prices.append(f"Entry: <code>{format_price(event.entry_price)}</code>")
    if event.exit_price is not None:
        prices.append(f"Exit: <code>{format_price(event.exit_price)}</code>")
    if prices:
        lines += ["", *prices]

    result = []
    pct = event.price_pnl_pct
    if pct is not None:
        result.append(f"Price PnL: <b>{format_pct(pct)}</b>")
    if event.realized_pnl is not None:
        result.append(f"Realized PnL: <b>{format_money(event.realized_pnl)}</b>")
    if event.fees is not None:
        result.append(f"Fees: {format_money(event.fees, signed=False)}")
    if event.funding is not None and event.funding != 0:
        # Stored as paid (+) / received (-); shown as impact on the result.
        result.append(f"Funding: {format_money(-event.funding)}")
    if event.net_pnl is not None:
        result.append(f"Net PnL: <b>{format_money(event.net_pnl)}</b>")
    elif event.bybit_closed_pnl is not None:
        result.append(f"Net PnL (Bybit): <b>{format_money(event.bybit_closed_pnl)}</b>")
    if result:
        lines += ["", *result]

    lines.append("")
    lines.append(f"Время сделки: {format_duration(event.duration)}")
    if event.balance_after is not None:
        lines.append(f"Баланс: <b>{format_money(event.balance_after, signed=False)}</b>")
    else:
        lines.append("Баланс: недоступен")
    if event.recovered:
        lines.append("<i>Восстановлено после перезапуска</i>")
    if event.mode != "mainnet":
        lines.append(f"<i>{html.escape(event.mode.upper())}</i>")
    return "\n".join(lines)
