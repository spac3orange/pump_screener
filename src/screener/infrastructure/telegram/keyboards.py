from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from screener.config import ScreenerSettings


def settings_keyboard(settings: ScreenerSettings) -> InlineKeyboardMarkup:
    def flag(v: bool) -> str:
        return "ON" if v else "OFF"

    rows = [
        [
            InlineKeyboardButton(
                text=f"Pump: {flag(settings.enable_pump)}",
                callback_data="toggle:enable_pump",
            ),
            InlineKeyboardButton(
                text=f"Dump: {flag(settings.enable_dump)}",
                callback_data="toggle:enable_dump",
            ),
        ],
        [
            InlineKeyboardButton(
                text=f"VolSpike: {flag(settings.enable_volume_spike)}",
                callback_data="toggle:enable_volume_spike",
            ),
        ],
        [
            InlineKeyboardButton(text="Pump +0.5%", callback_data="adj:pump:+0.5"),
            InlineKeyboardButton(text="Pump -0.5%", callback_data="adj:pump:-0.5"),
        ],
        [
            InlineKeyboardButton(text="Dump +0.5%", callback_data="adj:dump:+0.5"),
            InlineKeyboardButton(text="Dump -0.5%", callback_data="adj:dump:-0.5"),
        ],
        [
            InlineKeyboardButton(text="Vol x +0.5", callback_data="adj:vol:+0.5"),
            InlineKeyboardButton(text="Vol x -0.5", callback_data="adj:vol:-0.5"),
        ],
        [
            InlineKeyboardButton(text="Cooldown +60s", callback_data="adj:cooldown:+60"),
            InlineKeyboardButton(text="Cooldown -60s", callback_data="adj:cooldown:-60"),
        ],
        [
            InlineKeyboardButton(
                text="MinTurnover +50k", callback_data="adj:turnover:+50000"
            ),
            InlineKeyboardButton(
                text="MinTurnover -50k", callback_data="adj:turnover:-50000"
            ),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)
