from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from screener.config import ScreenerSettings
from screener.domain.exchanges import ALL_EXCHANGES, BINANCE, BITGET, EXCHANGES


def _flag(enabled: bool) -> str:
    return "ON" if enabled else "OFF"


def trading_keyboard(*, enabled: bool, mainnet: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if enabled:
        rows.append(
            [InlineKeyboardButton(text="🔴 Выключить торговлю", callback_data="trading:off")]
        )
    elif mainnet:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🟢 Включить торговлю (MAINNET)",
                    callback_data="trading:enable_ask",
                )
            ]
        )
    else:
        rows.append(
            [InlineKeyboardButton(text="🟢 Включить торговлю", callback_data="trading:on")]
        )
    rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="trading:refresh")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def trading_mainnet_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Подтвердить MAINNET", callback_data="trading:enable_confirm"
                ),
                InlineKeyboardButton(text="Отмена", callback_data="trading:enable_cancel"),
            ]
        ]
    )


def _exchange_shown(code: str, *, binance_available: bool, bitget_available: bool) -> bool:
    if code == BINANCE:
        return binance_available
    if code == BITGET:
        return bitget_available
    return True


def settings_keyboard(
    settings: ScreenerSettings,
    exchanges: frozenset[str] | None = None,
    *,
    binance_available: bool = True,
    bitget_available: bool = True,
) -> InlineKeyboardMarkup:
    chosen = exchanges if exchanges is not None else frozenset(ALL_EXCHANGES)
    rows = [
        [
            InlineKeyboardButton(
                text=f"Лонг {_flag(settings.enable_long)}", callback_data="toggle:long"
            ),
            InlineKeyboardButton(
                text=f"Дамп {_flag(settings.enable_dump)}", callback_data="toggle:dump"
            ),
        ],
        [
            InlineKeyboardButton(text="🟢 Период лонг", callback_data="set:long:period"),
            InlineKeyboardButton(text="➗ Процент лонг", callback_data="set:long:pct"),
        ],
        [
            InlineKeyboardButton(text="🔻 Период дамп", callback_data="set:dump:period"),
            InlineKeyboardButton(text="➗ Процент дамп", callback_data="set:dump:pct"),
        ],
        [
            InlineKeyboardButton(
                text="💧 Мин. оборот 24ч", callback_data="set:turnover:value"
            )
        ],
        [
            InlineKeyboardButton(
                text=f"Cooldown {settings.cooldown_sec}s", callback_data="set:antispam:cooldown"
            ),
            InlineKeyboardButton(
                text=f"Escalate +{settings.escalate_step_pct:g}%",
                callback_data="set:antispam:escalate",
            ),
        ],
        [
            InlineKeyboardButton(
                text=f"Reset {settings.reset_ratio:g}",
                callback_data="set:antispam:reset_ratio",
            ),
            InlineKeyboardButton(
                text=f"Reset hold {settings.reset_hold_sec}s",
                callback_data="set:antispam:reset_hold",
            ),
        ],
        [
            InlineKeyboardButton(
                text="❓ Anti-spam справка", callback_data="antispam:help"
            )
        ],
        [
            InlineKeyboardButton(
                text=f"{EXCHANGES[code].label} {'ON' if code in chosen else 'OFF'}",
                callback_data=f"ex:{code}",
            )
            for code in ALL_EXCHANGES
            if _exchange_shown(
                code, binance_available=binance_available, bitget_available=bitget_available
            )
        ],
        [
            InlineKeyboardButton(
                text=f"Лимиты монет ({len(settings.symbol_daily_limits)})",
                callback_data="limits:list",
            )
        ],
        [InlineKeyboardButton(text="📁 Профили", callback_data="prof:list")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def symbol_limits_keyboard(settings: ScreenerSettings) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="➕ Добавить лимит", callback_data="limits:add")],
    ]
    for symbol in sorted(settings.symbol_daily_limits):
        max_n = settings.symbol_daily_limits[symbol]
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{symbol}: {max_n}/сутки",
                    callback_data=f"limits:del:{symbol}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="limits:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
