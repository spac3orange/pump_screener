from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from screener.config import ScreenerSettings


def _flag(enabled: bool) -> str:
    return "ON" if enabled else "OFF"


def settings_keyboard(settings: ScreenerSettings) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"Лонг {_flag(settings.enable_long)}", callback_data="toggle:long"
            ),
            InlineKeyboardButton(
                text=f"Шорт {_flag(settings.enable_short)}", callback_data="toggle:short"
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
            InlineKeyboardButton(text="🔴 Период шорт", callback_data="set:short:period"),
            InlineKeyboardButton(text="➗ Процент шорт", callback_data="set:short:pct"),
        ],
        [
            InlineKeyboardButton(text="🔻 Период дамп", callback_data="set:dump:period"),
            InlineKeyboardButton(text="➗ Процент дамп", callback_data="set:dump:pct"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)
