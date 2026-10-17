"""Subscriber commands. Identity always comes from ``from_user`` via the middleware
(``account``); callback data never names a user, so it cannot address someone else.
No handler here can reach the trading module.
"""

from __future__ import annotations

import html
import logging
from typing import TYPE_CHECKING, Any

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart, Filter
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardRemove,
    TelegramObject,
)
from pydantic import ValidationError

from screener.application.commercial.payment_service import InvoiceError, SubscriptionChangeError
from screener.application.commercial.trial_service import TrialResult
from screener.domain.commercial import UserAccount, UserDetectionConfig
from screener.infrastructure.telegram import commercial_texts as texts
from screener.infrastructure.telegram.settings_input import (
    FIELDS,
    RULES,
    TURNOVER_HINT,
    SettingInputError,
    apply_setting_text,
    parse_turnover,
    prompt_for,
    toggle_rule,
)
from screener.infrastructure.telegram.user_keyboards import (
    MENU_ACCOUNT,
    MENU_BUTTONS,
    MENU_HELP,
    MENU_SETTINGS,
    USER_ACTION,
    USER_SETTINGS,
    account_keyboard,
    cancel_confirm_keyboard,
    pay_keyboard,
    reset_confirm_keyboard,
    start_keyboard,
    subscription_keyboard,
    terms_keyboard,
    user_settings_keyboard,
)

if TYPE_CHECKING:
    from screener.app_state import AppState
    from screener.commercial_runtime import CommercialRuntime

logger = logging.getLogger(__name__)

USER_EDIT = "us"


class HasAccount(Filter):
    """Commercial handlers need a registered account (absent if the DB was unavailable)."""

    async def __call__(self, event: TelegramObject, **data: Any) -> bool:
        return data.get("account") is not None and data.get("commercial") is not None


user_router = Router(name="commercial-users")
user_router.message.filter(HasAccount())
user_router.callback_query.filter(HasAccount())


async def _edit(query: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None) -> None:
    if not isinstance(query.message, Message):
        return
    try:
        await query.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        if "not modified" not in str(exc):
            raise


def _terms_ok(account: UserAccount, rt: CommercialRuntime) -> bool:
    return account.is_admin or account.terms_accepted(rt.settings.terms_version)


# --- commands -------------------------------------------------------------------


@user_router.message(CommandStart())
async def u_start(message: Message, account: UserAccount, commercial: CommercialRuntime) -> None:
    view = await commercial.users.account_view(account)
    terms_needed = not _terms_ok(account, commercial)
    await message.answer(
        texts.welcome_text(
            commercial.settings, terms_needed=terms_needed, trial=view.trial_available
        ),
        reply_markup=terms_keyboard() if terms_needed else start_keyboard(view),
    )


@user_router.message(Command("account"))
async def u_account(message: Message, account: UserAccount, commercial: CommercialRuntime) -> None:
    view = await commercial.users.account_view(account)
    await message.answer(texts.account_text(view), reply_markup=account_keyboard(view))


@user_router.message(Command("settings"))
async def u_settings(message: Message, account: UserAccount, commercial: CommercialRuntime) -> None:
    cfg = await commercial.users.get_config(account.id)
    await message.answer(texts.SETTINGS_INTRO_TEXT)
    await message.answer(texts.settings_text(cfg), reply_markup=user_settings_keyboard(cfg))


@user_router.message(Command("subscription"))
async def u_subscription(
    message: Message, account: UserAccount, commercial: CommercialRuntime
) -> None:
    view = await commercial.users.account_view(account)
    await message.answer(
        texts.subscription_text(view, commercial.settings),
        reply_markup=subscription_keyboard(view, commercial.settings),
    )


@user_router.message(Command("terms"))
async def u_terms(message: Message, account: UserAccount, commercial: CommercialRuntime) -> None:
    markup = None if _terms_ok(account, commercial) else terms_keyboard()
    await message.answer(texts.terms_text(commercial.settings), reply_markup=markup)


@user_router.message(Command("support"))
async def u_support(message: Message, commercial: CommercialRuntime) -> None:
    await message.answer(texts.support_text(commercial.settings))


@user_router.message(Command("paysupport"))
async def u_paysupport(message: Message, commercial: CommercialRuntime) -> None:
    await message.answer(texts.paysupport_text(commercial.settings))


@user_router.message(Command("help"))
async def u_help(message: Message, commercial: CommercialRuntime) -> None:
    await message.answer(texts.help_text(commercial.settings))


# --- account / subscription callbacks ------------------------------------------


@user_router.callback_query(F.data.startswith(f"{USER_ACTION}:"))
async def u_action(
    query: CallbackQuery, account: UserAccount, commercial: CommercialRuntime
) -> None:
    assert query.data is not None
    action = query.data.split(":", 1)[1]
    users = commercial.users

    if action == "terms":
        account = await users.accept_terms(account) or account
        view = await users.account_view(account)
        await _edit(
            query,
            texts.welcome_text(commercial.settings, terms_needed=False, trial=view.trial_available),
            start_keyboard(view),
        )
        await query.answer("Условия приняты")
        return

    if action == "account":
        view = await users.account_view(account)
        await _edit(query, texts.account_text(view), account_keyboard(view))
        await query.answer()
        return

    if action == "mon":
        account = await users.set_monitoring(account, not account.monitoring_enabled) or account
        view = await users.account_view(account)
        await _edit(query, texts.account_text(view), account_keyboard(view))
        await query.answer(
            "Сигналы включены" if account.monitoring_enabled else "Сигналы выключены"
        )
        return

    if action == "trial":
        await _trial(query, account, commercial)
        return

    if action == "sub":
        view = await users.account_view(account)
        await _edit(
            query,
            texts.subscription_text(view, commercial.settings),
            subscription_keyboard(view, commercial.settings),
        )
        await query.answer()
        return

    if action == "buy":
        await _buy(query, account, commercial)
        return

    if action == "cancel":
        await _edit(
            query,
            "Отменить автопродление? Доступ сохранится до конца оплаченного периода.",
            cancel_confirm_keyboard(),
        )
        await query.answer()
        return

    if action in ("cancel_ok", "resume"):
        payments = commercial.payments
        if action == "cancel_ok":
            _, err = await payments.cancel_autorenew(account)
        else:
            _, err = await payments.resume_autorenew(account)
        if err is SubscriptionChangeError.GATEWAY:
            await query.answer("Telegram не ответил, попробуйте позже.", show_alert=True)
            return
        if err is SubscriptionChangeError.NO_SUBSCRIPTION:
            await query.answer("Активной подписки нет.", show_alert=True)
        fresh = await users.get(account.id) or account
        view = await users.account_view(fresh)
        await _edit(
            query,
            texts.subscription_text(view, commercial.settings),
            subscription_keyboard(view, commercial.settings),
        )
        if err is None:
            await query.answer(
                "Автопродление отключено" if action == "cancel_ok" else "Автопродление включено"
            )
        elif err is SubscriptionChangeError.ALREADY:
            await query.answer("Уже сделано")
        return

    await query.answer("Неизвестное действие", show_alert=True)


async def _trial(query: CallbackQuery, account: UserAccount, rt: CommercialRuntime) -> None:
    result, account = await rt.trials.start(account)
    if result is TrialResult.STARTED:
        await query.answer("Пробный период начался")
        if isinstance(query.message, Message):
            await query.message.answer(texts.trial_started_text(account))
        return
    messages = {
        TrialResult.TERMS_REQUIRED: texts.TERMS_REQUIRED_TEXT,
        TrialResult.ALREADY_USED: "Пробный период уже был использован.",
        TrialResult.ALREADY_PAID: "Пробный период недоступен после оплаты подписки.",
        TrialResult.NOT_AVAILABLE: "Пробный период недоступен.",
    }
    await query.answer(messages[result], show_alert=True)


async def _buy(query: CallbackQuery, account: UserAccount, rt: CommercialRuntime) -> None:
    result = await rt.payments.create_invoice(account)
    if result.link is None:
        messages = {
            InvoiceError.ADMIN: "Администратору подписка не нужна.",
            InvoiceError.TERMS_REQUIRED: texts.TERMS_REQUIRED_TEXT,
            InvoiceError.ALREADY_SUBSCRIBED: "Подписка уже активна: /subscription",
            InvoiceError.RATE_LIMITED: "Слишком много счетов за час. Попробуйте позже.",
            InvoiceError.GATEWAY: "Не удалось создать счёт. Попробуйте позже.",
        }
        assert result.error is not None
        await query.answer(messages[result.error], show_alert=True)
        return
    await query.answer()
    if isinstance(query.message, Message):
        await query.message.answer(
            f"Счёт на <b>{rt.settings.subscription_price_stars} ⭐</b> "
            f"({html.escape(rt.settings.subscription_title)}, 30 дней, автопродление).\n"
            f"Счёт действует {rt.settings.invoice_ttl_hours} ч.",
            reply_markup=pay_keyboard(result.link, rt.settings),
        )


# --- settings callbacks -----------------------------------------------------------


@user_router.callback_query(F.data.startswith(f"{USER_SETTINGS}:"))
async def u_settings_action(
    query: CallbackQuery, account: UserAccount, commercial: CommercialRuntime, state: AppState
) -> None:
    assert query.data is not None and query.from_user is not None
    parts = query.data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    users = commercial.users

    if action == "menu":
        cfg = await users.get_config(account.id)
        if isinstance(query.message, Message):
            await query.message.answer(texts.SETTINGS_INTRO_TEXT)
            await query.message.answer(
                texts.settings_text(cfg), reply_markup=user_settings_keyboard(cfg)
            )
        await query.answer()
        return

    if action == "t" and len(parts) == 3 and parts[2] in RULES:
        cfg = await users.get_config(account.id)
        updated = toggle_rule(cfg, parts[2])
        await users.update_config(account, updated)
        await _edit(query, texts.settings_text(updated), user_settings_keyboard(updated))
        await query.answer("Обновлено")
        return

    if action == "s" and len(parts) == 4:
        kind, field_name = parts[2], parts[3]
        cfg = await users.get_config(account.id)
        if kind == "turnover" and field_name == "value":
            prompt = f"{TURNOVER_HINT}\nСейчас: {texts.format_turnover(cfg.min_turnover_24h_usdt)}."
        elif kind in RULES and field_name in FIELDS:
            prompt = prompt_for(kind, field_name, cfg)
        else:
            await query.answer("Неизвестная настройка", show_alert=True)
            return
        state.begin_setting_edit(query.from_user.id, USER_EDIT, f"{kind}:{field_name}")
        if isinstance(query.message, Message):
            await query.message.answer(prompt)
        await query.answer()
        return

    if action == "reset":
        await _edit(
            query, "Сбросить ваши правила к значениям по умолчанию?", reset_confirm_keyboard()
        )
        await query.answer()
        return

    if action == "reset_ok":
        cfg = await users.reset_config(account)
        await _edit(query, texts.settings_text(cfg), user_settings_keyboard(cfg))
        await query.answer("Настройки сброшены")
        return

    await query.answer("Неизвестное действие", show_alert=True)


@user_router.message(F.text.in_(MENU_BUTTONS))
async def u_menu_button(
    message: Message, account: UserAccount, commercial: CommercialRuntime, state: AppState
) -> None:
    assert message.from_user is not None
    state.clear_pending_edit(message.from_user.id)
    await message.answer(texts.MENU_REMOVED_TEXT, reply_markup=ReplyKeyboardRemove())
    if message.text == MENU_ACCOUNT:
        await u_account(message, account, commercial)
    elif message.text == MENU_SETTINGS:
        await u_settings(message, account, commercial)
    elif message.text == MENU_HELP:
        await u_help(message, commercial)
    else:
        await u_subscription(message, account, commercial)


@user_router.message(F.text, ~F.text.startswith("/"))
async def u_setting_value(
    message: Message, account: UserAccount, commercial: CommercialRuntime, state: AppState
) -> None:
    assert message.from_user is not None and message.text is not None
    pending = state.pending_edit(message.from_user.id)
    if pending is None or pending[0] != USER_EDIT:
        return
    kind, _, field_name = pending[1].partition(":")
    users = commercial.users
    cfg = await users.get_config(account.id)
    try:
        if kind == "turnover":
            value = parse_turnover(message.text)
            updated = cfg.model_copy(update={"min_turnover_24h_usdt": value})
        else:
            updated = apply_setting_text(cfg, kind, field_name, message.text)
        new_cfg = UserDetectionConfig.model_validate(updated.model_dump())
    except (SettingInputError, ValidationError) as exc:
        hint = TURNOVER_HINT if kind == "turnover" else prompt_for(kind, field_name, cfg)
        reason = str(exc) if isinstance(exc, SettingInputError) else "Недопустимое значение."
        await message.answer(f"{html.escape(reason)}\n{hint}")
        return
    state.clear_pending_edit(message.from_user.id)
    await users.update_config(account, new_cfg)
    await message.answer(texts.settings_text(new_cfg), reply_markup=user_settings_keyboard(new_cfg))


@user_router.message(F.text.startswith("/"))
async def u_unknown_command(message: Message) -> None:
    await message.answer(texts.UNKNOWN_COMMAND_TEXT)
