from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from screener.config import ScreenerSettings


def settings_keyboard(settings: ScreenerSettings) -> InlineKeyboardMarkup:
    def flag(v: bool) -> str:
        return "ON" if v else "OFF"

    rows = [
        [
            InlineKeyboardButton(
                text=f"Лонг: {flag(settings.enable_long)}",
                callback_data="toggle:enable_long",
            ),
            InlineKeyboardButton(
                text=f"Шорт: {flag(settings.enable_short)}",
                callback_data="toggle:enable_short",
            ),
            InlineKeyboardButton(
                text=f"Дамп: {flag(settings.enable_dump)}",
                callback_data="toggle:enable_dump",
            ),
        ],
        [
            InlineKeyboardButton(text="Лонг +1%", callback_data="adj:long:+1"),
            InlineKeyboardButton(text="Лонг -1%", callback_data="adj:long:-1"),
        ],
        [
            InlineKeyboardButton(text="Шорт +1%", callback_data="adj:short:+1"),
            InlineKeyboardButton(text="Шорт -1%", callback_data="adj:short:-1"),
        ],
        [
            InlineKeyboardButton(text="Дамп +1%", callback_data="adj:dump:+1"),
            InlineKeyboardButton(text="Дамп -1%", callback_data="adj:dump:-1"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)
