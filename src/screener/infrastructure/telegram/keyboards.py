from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from screener.config import ScreenerSettings


def settings_keyboard(_settings: ScreenerSettings) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="🟢 Период лонг", callback_data="set:long:period"),
            InlineKeyboardButton(text="➗ Процент лонг", callback_data="set:long:pct"),
        ],
        [
            InlineKeyboardButton(text="🔴 Период шорт", callback_data="set:short:period"),
            InlineKeyboardButton(text="➗ Процент шорт", callback_data="set:short:pct"),
        ],
        [
            InlineKeyboardButton(text="🔻 Период дамп", callback_data="set:dump:period"),
            InlineKeyboardButton(text="➗ Процент дамп", callback_data="set:dump:pct"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)
