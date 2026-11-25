"""Inline keyboards for subscribers. Callback data never carries user ids."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from screener.application.commercial.user_service import AccountView
from screener.config import CommercialSettings
from screener.domain.commercial import AccessKind, UserDetectionConfig
from screener.domain.exchanges import ALL_EXCHANGES, BINANCE, BITGET, EXCHANGES

USER_SETTINGS = "us"
USER_ACTION = "ua"

# Texts of the retired reply keyboard; clients that still show it keep sending them.
MENU_ACCOUNT = "👤 Аккаунт"
MENU_SETTINGS = "⚙️ Настройки"
MENU_SUBSCRIPTION = "⭐ Подписка"
MENU_HELP = "❓ Помощь"  # legacy label; still accepted to clear old reply keyboards
MENU_BUTTONS = (MENU_ACCOUNT, MENU_SETTINGS, MENU_SUBSCRIPTION, MENU_HELP)


def _btn(text: str, data: str, *, style: str | None = None) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data, style=style)


def _flag(enabled: bool) -> str:
    return "ON" if enabled else "OFF"


def terms_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[_btn("✅ Принимаю условия", f"{USER_ACTION}:terms")]]
    )


def start_keyboard(view: AccountView) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if view.trial_available and not view.access.active:
        rows.append(
            [_btn("🎁 Начать пробный период", f"{USER_ACTION}:trial", style="success")]
        )
    if view.access.kind is not AccessKind.ADMIN and view.subscription is None:
        rows.append([_btn("⭐ Оформить подписку", f"{USER_ACTION}:sub", style="success")])
    rows.append(
        [
            _btn("👤 Аккаунт", f"{USER_ACTION}:account"),
            _btn("⚙️ Настройки", f"{USER_SETTINGS}:menu"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def account_keyboard(view: AccountView) -> InlineKeyboardMarkup:
    mon = "⏸ Выключить сигналы" if view.user.monitoring_enabled else "▶️ Включить сигналы"
    rows = [[_btn(mon, f"{USER_ACTION}:mon")]]
    if view.trial_available and not view.access.active:
        rows.append(
            [_btn("🎁 Начать пробный период", f"{USER_ACTION}:trial", style="success")]
        )
    rows.append(
        [
            _btn("⚙️ Настройки", f"{USER_SETTINGS}:menu"),
            _btn("⭐ Подписка", f"{USER_ACTION}:sub"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def subscription_keyboard(view: AccountView, settings: CommercialSettings) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    sub = view.subscription
    if view.access.kind is AccessKind.ADMIN:
        pass
    elif sub is None:
        rows.append(
            [
                _btn(
                    f"⭐ Оплатить {settings.subscription_price_stars} Stars",
                    f"{USER_ACTION}:buy",
                    style="success",
                )
            ]
        )
        if view.trial_available and not view.access.active:
            rows.append([_btn("🎁 Пробный период", f"{USER_ACTION}:trial", style="success")])
    elif sub.cancel_at_period_end:
        rows.append([_btn("🔄 Включить автопродление", f"{USER_ACTION}:resume")])
    else:
        rows.append([_btn("✖️ Отменить автопродление", f"{USER_ACTION}:cancel")])
    rows.append([_btn("👤 Аккаунт", f"{USER_ACTION}:account")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cancel_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _btn("Да, отменить", f"{USER_ACTION}:cancel_ok"),
                _btn("Нет", f"{USER_ACTION}:sub"),
            ]
        ]
    )


def pay_keyboard(link: str, settings: CommercialSettings) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"⭐ Оплатить {settings.subscription_price_stars} Stars",
                    url=link,
                    style="success",
                )
            ]
        ]
    )


def _exchange_shown(code: str, *, binance_available: bool, bitget_available: bool) -> bool:
    if code == BINANCE:
        return binance_available
    if code == BITGET:
        return bitget_available
    return True


def user_settings_keyboard(
    cfg: UserDetectionConfig,
    exchanges: frozenset[str] | None = None,
    *,
    binance_available: bool = True,
    bitget_available: bool = True,
) -> InlineKeyboardMarkup:
    p = USER_SETTINGS
    chosen = exchanges if exchanges is not None else frozenset(ALL_EXCHANGES)
    rows = [
        [
            _btn(f"Лонг {_flag(cfg.enable_long)}", f"{p}:t:long"),
            _btn(f"Дамп {_flag(cfg.enable_dump)}", f"{p}:t:dump"),
        ],
        [_btn("🟢 Период лонг", f"{p}:s:long:period"), _btn("➗ % лонг", f"{p}:s:long:pct")],
        [_btn("🔻 Период дамп", f"{p}:s:dump:period"), _btn("➗ % дамп", f"{p}:s:dump:pct")],
        [_btn("💧 Мин. оборот 24ч", f"{p}:s:turnover:value")],
        [
            _btn(
                f"{EXCHANGES[code].label} {_flag(code in chosen)}",
                f"{p}:x:{code}",
            )
            for code in ALL_EXCHANGES
            if _exchange_shown(
                code, binance_available=binance_available, bitget_available=bitget_available
            )
        ],
        [_btn("↩️ Сбросить по умолчанию", f"{p}:reset", style="danger")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def reset_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _btn("Сбросить", f"{USER_SETTINGS}:reset_ok"),
                _btn("Отмена", f"{USER_SETTINGS}:menu"),
            ]
        ]
    )
