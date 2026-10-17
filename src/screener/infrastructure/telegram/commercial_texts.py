"""User-facing texts of the commercial bot (HTML parse mode)."""

from __future__ import annotations

import html
from datetime import datetime

from screener.application.commercial.user_service import AccountView
from screener.config import CommercialSettings
from screener.domain.commercial import AccessKind, UserAccount, UserDetectionConfig

PRODUCT = "PumpScreener"


def _dt(value: datetime | None) -> str:
    return f"{value:%Y-%m-%d %H:%M} UTC" if value is not None else "—"


def _turnover(value: float) -> str:
    if value >= 1_000_000 and value % 1_000_000 == 0:
        return f"{value / 1_000_000:g}M"
    if value >= 1_000 and value % 1_000 == 0:
        return f"{value / 1_000:g}K"
    return f"{value:g}"


def format_turnover(value: float) -> str:
    return f"${_turnover(value)}"


def rules_text(cfg: UserDetectionConfig) -> str:
    def line(emoji: str, name: str, on: bool, pct: float, interval: int, sign: str = "") -> str:
        state = "" if on else " (OFF)"
        return f"{emoji} {name}: {sign}{pct:g}% за {interval // 60} мин.{state}"

    return "\n".join(
        [
            line(
                "🟢", "Лонг (рост)", cfg.enable_long, cfg.long_threshold_pct, cfg.long_interval_sec
            ),
            line(
                "🔴",
                "Шорт (падение)",
                cfg.enable_short,
                cfg.short_threshold_pct,
                cfg.short_interval_sec,
            ),
            line("🔻", "Дамп", cfg.enable_dump, cfg.dump_threshold_pct, cfg.dump_interval_sec, "-"),
            f"💧 Мин. оборот 24ч: {format_turnover(cfg.min_turnover_24h_usdt)}",
        ]
    )


def welcome_text(settings: CommercialSettings, *, terms_needed: bool, trial: bool) -> str:
    lines = [
        f"<b>{PRODUCT}</b> — сигналы о резких движениях цены на Bybit "
        "(USDT Perpetual) по вашим правилам.",
        "",
        "Вы сами задаёте процент и период для лонга, шорта и дампа в /settings.",
    ]
    if trial:
        lines.append(
            f"\nДоступен бесплатный пробный период: <b>{settings.trial_duration_hours} ч</b> "
            "(один раз на аккаунт). Он начнётся только после нажатия кнопки."
        )
    lines.append(
        f"\nПодписка {html.escape(settings.subscription_title)}: "
        f"<b>{settings.subscription_price_stars} ⭐</b> за 30 дней, с автопродлением."
    )
    if terms_needed:
        lines.append("\nЧтобы продолжить, ознакомьтесь с условиями (/terms) и примите их.")
    lines.append("\n<i>Сигналы не являются инвестиционной рекомендацией.</i>")
    return "\n".join(lines)


def access_line(view: AccountView) -> str:
    access = view.access
    if access.kind is AccessKind.ADMIN:
        return "Доступ: 🛡 администратор"
    if access.kind is AccessKind.PRO:
        return f"Доступ: ⭐ Pro до {_dt(access.until)}"
    if access.kind is AccessKind.TRIAL:
        return f"Доступ: 🎁 пробный период до {_dt(access.until)}"
    return "Доступ: ❌ нет активного доступа"


def account_text(view: AccountView) -> str:
    user = view.user
    monitoring = "🟢 включены" if user.monitoring_enabled else "⚪️ выключены"
    lines = [
        "<b>Мой аккаунт</b>",
        access_line(view),
        f"Сигналы: {monitoring}",
    ]
    sub = view.subscription
    if sub is not None:
        renew = "выключено" if sub.cancel_at_period_end else "включено"
        lines.append(f"Автопродление: {renew}")
    if view.trial_available:
        lines.append("Пробный период: доступен")
    elif user.trial_used:
        lines.append("Пробный период: использован")
    lines += ["", "<b>Мои правила</b>", rules_text(view.config)]
    if not view.access.active:
        lines.append("\nСигналы приходят только при активном доступе: /subscription")
    return "\n".join(lines)


def settings_text(cfg: UserDetectionConfig) -> str:
    return (
        "<b>Мои настройки сигналов</b>\n"
        f"{rules_text(cfg)}\n\n"
        "<i>Изменения применяются сразу. Пауза между повторами одной монеты и "
        "прочие системные параметры одинаковы для всех.</i>"
    )


def subscription_text(view: AccountView, settings: CommercialSettings) -> str:
    lines = [f"<b>{html.escape(settings.subscription_title)}</b>", access_line(view)]
    sub = view.subscription
    if sub is not None:
        if sub.cancel_at_period_end:
            lines.append(
                f"Автопродление выключено — доступ закончится {_dt(sub.current_period_end)}."
            )
        else:
            lines.append(f"Следующее списание: {_dt(sub.current_period_end)}")
        lines.append(f"Стоимость: {settings.subscription_price_stars} ⭐ / 30 дней")
    else:
        lines.append(
            f"\nСтоимость: <b>{settings.subscription_price_stars} ⭐</b> за 30 дней. "
            "Оплата Telegram Stars, продлевается автоматически каждые 30 дней; "
            "отменить автопродление можно здесь в любой момент."
        )
        if view.trial_available:
            lines.append(
                f"Или начните бесплатный пробный период на {settings.trial_duration_hours} ч."
            )
    return "\n".join(lines)


def terms_text(settings: CommercialSettings) -> str:
    link = (
        f'\nПолный текст: <a href="{html.escape(settings.terms_url, quote=True)}">условия</a>'
        if settings.terms_url
        else ""
    )
    return (
        f"<b>Условия использования {PRODUCT}</b> (версия {html.escape(settings.terms_version)})\n\n"
        "1. Бот присылает автоматические уведомления о движении цены. Это не "
        "инвестиционная рекомендация; решения и риски — ваши.\n"
        "2. Сигналы могут задерживаться или не приходить из-за сбоев бирж, сети или Telegram.\n"
        f"3. Пробный период {settings.trial_duration_hours} ч предоставляется один раз.\n"
        f"4. Подписка {settings.subscription_price_stars} ⭐ за 30 дней продлевается "
        "автоматически, пока вы не отключите автопродление в /subscription. "
        "После отмены доступ сохраняется до конца оплаченного периода.\n"
        "5. Вопросы по оплате и возвратам — /paysupport."
        f"{link}"
    )


def support_text(settings: CommercialSettings) -> str:
    contact = html.escape(settings.support_contact) if settings.support_contact else "—"
    return f"<b>Поддержка</b>\nПишите: {contact}"


def paysupport_text(settings: CommercialSettings) -> str:
    contact = html.escape(settings.support_contact) if settings.support_contact else "—"
    return (
        "<b>Вопросы по оплате</b>\n"
        "Напишите в поддержку и укажите дату оплаты. Возвраты Telegram Stars "
        "рассматриваются вручную.\n"
        f"Контакт: {contact}\n\n"
        "Обратите внимание: поддержка Telegram не может помочь с покупками в этом боте."
    )


def help_text(settings: CommercialSettings) -> str:
    return (
        f"<b>{PRODUCT} — справка</b>\n\n"
        "Бот следит за всеми USDT Perpetual на Bybit и присылает сигнал, когда цена "
        "меняется на заданный процент за заданный период:\n"
        "🟢 <b>Лонг</b> — рост на X% за N минут\n"
        "🔴 <b>Шорт</b> — падение на X% за N минут\n"
        "🔻 <b>Дамп</b> — сильное падение\n"
        "Монеты с оборотом за 24ч ниже порога пропускаются.\n\n"
        "<b>Команды</b>\n"
        "/start — начало\n"
        "/account — мой доступ и статус\n"
        "/settings — мои правила сигналов\n"
        "/subscription — подписка и оплата\n"
        "/terms — условия\n"
        "/support — поддержка\n"
        "/paysupport — вопросы по оплате\n"
        "/help — эта справка\n\n"
        f"Пробный период: {settings.trial_duration_hours} ч, один раз. "
        f"Подписка: {settings.subscription_price_stars} ⭐ / 30 дней."
    )


def trial_started_text(user: UserAccount) -> str:
    return (
        "🎁 <b>Пробный период начался</b>\n"
        f"Сигналы будут приходить до {_dt(user.trial_ends_at)}.\n"
        "Настроить правила: /settings"
    )


def access_ended_text(user: UserAccount, settings: CommercialSettings) -> str:
    trial = user.trial_ends_at is not None and user.trial_ends_at == user.access_until
    what = "Пробный период закончился" if trial else "Подписка закончилась"
    return (
        f"⏳ <b>{what}.</b>\n"
        "Сигналы больше не отправляются.\n"
        f"Продлить доступ: /subscription ({settings.subscription_price_stars} ⭐ / 30 дней)"
    )


def payment_applied_text(period_end: datetime | None, *, renewal: bool) -> str:
    if renewal:
        return f"✅ Подписка продлена до {_dt(period_end)}."
    return (
        "✅ <b>Оплата получена.</b>\n"
        f"Подписка активна до {_dt(period_end)}, автопродление включено.\n"
        "Настроить правила: /settings"
    )


PAYMENT_PROBLEM_TEXT = (
    "Оплата получена, но не может быть применена автоматически. "
    "Мы уже разбираемся; при необходимости средства будут возвращены. /paysupport"
)
REFUNDED_TEXT = "Возврат оформлен, доступ по подписке отключён."
UNKNOWN_COMMAND_TEXT = "Неизвестная команда. Список команд: /help"
MENU_REMOVED_TEXT = "Кнопки внизу убраны. Все команды — в меню слева от поля ввода."
RATE_LIMITED_TEXT = "Слишком много запросов, подождите немного."
TERMS_REQUIRED_TEXT = "Сначала примите условия использования: /terms"
