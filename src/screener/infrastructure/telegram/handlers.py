from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from screener.application.metrics import metrics
from screener.config import ScreenerSettings
from screener.infrastructure.telegram.keyboards import settings_keyboard

if TYPE_CHECKING:
    from screener.app_state import AppState

router = Router()


def _rule_line(
    emoji: str,
    name: str,
    pct: float,
    interval_sec: int,
    enabled: bool,
    *,
    signed: bool = False,
) -> str:
    value = f"-{pct:g}" if signed else f"{pct:g}"
    state = "" if enabled else " (OFF)"
    return f"{emoji} {name}: {value}% за {interval_sec // 60} мин.{state}"


def _settings_text(settings: ScreenerSettings) -> str:
    lines = [
        "<b>Текущие настройки бота:</b>",
        _rule_line(
            "🟢",
            "Лонг",
            settings.long_threshold_pct,
            settings.long_interval_sec,
            settings.enable_long,
        ),
        _rule_line(
            "🔴",
            "Шорт",
            settings.short_threshold_pct,
            settings.short_interval_sec,
            settings.enable_short,
        ),
        _rule_line(
            "🔻",
            "Дамп",
            settings.dump_threshold_pct,
            settings.dump_interval_sec,
            settings.enable_dump,
            signed=True,
        ),
    ]
    if settings.exclude_symbols:
        lines.append(f"Исключения: {', '.join(sorted(settings.exclude_symbols))}")
    return "\n".join(lines)


@router.message(CommandStart())
async def cmd_start(message: Message, state: AppState) -> None:
    await message.answer(
        "<b>Bybit Market Screener</b>\n"
        "Мониторинг crypto USDT Linear Perpetual: лонг, шорт и дамп.\n\n"
        "Команды:\n"
        "/status — состояние\n"
        "/settings — настройки мониторинга\n"
        "/signals — последние сигналы\n"
        "/stats — статистика\n"
        "/help — справка"
    )


@router.message(Command("help"))
async def cmd_help(message: Message, state: AppState) -> None:
    await message.answer(
        "<b>Справка</b>\n"
        "/start — о сервисе\n"
        "/status — готовность, инструменты, WS\n"
        "/settings — пороги и переключатели (inline-кнопки)\n"
        "/signals — последние 10 сигналов из БД\n"
        "/stats — счётчики сигналов и ошибок\n\n"
        "Сигналы приходят автоматически при срабатывании правил.\n"
        "Доступ только для ADMIN_ID из .env."
    )


@router.message(Command("status"))
async def cmd_status(message: Message, state: AppState) -> None:
    snap = metrics.snapshot()
    admins = ", ".join(str(i) for i in state.system.admin_id) or "—"
    await message.answer(
        "<b>Статус</b>\n"
        f"Ready: {snap['ready']}\n"
        f"Instruments: {snap['instruments_active']}\n"
        f"WS connections: {snap['ws_connections']}\n"
        f"Msg/s: {snap['messages_per_sec']}\n"
        f"Signals (runtime): {snap['signals_total']}\n"
        f"Queue drops: {snap['queue_drops']}\n"
        f"Last msg age ms: {snap['last_message_age_ms']}\n"
        f"Admin IDs: <code>{admins}</code>"
    )


@router.message(Command("settings"))
async def cmd_settings(message: Message, state: AppState) -> None:
    settings = state.signal_engine.settings
    await message.answer(
        _settings_text(settings),
        reply_markup=settings_keyboard(settings),
    )


@router.message(Command("signals"))
async def cmd_signals(message: Message, state: AppState) -> None:
    rows = await state.repo.recent_signals(limit=10)
    if not rows:
        await message.answer("Сигналов пока нет.")
        return
    lines = [
        f"{row.detected_at.strftime('%H:%M:%S')} <b>{row.signal_type}</b> "
        f"{row.symbol} {row.change_pct:+.2f}% / {row.interval_sec // 60}m"
        for row in rows
    ]
    await message.answer("<b>Последние сигналы</b>\n" + "\n".join(lines))


@router.message(Command("stats"))
async def cmd_stats(message: Message, state: AppState) -> None:
    db_stats = await state.repo.signal_stats()
    snap = metrics.snapshot()
    by_type = ", ".join(f"{k}={v}" for k, v in db_stats.get("by_type", {}).items()) or "—"
    await message.answer(
        "<b>Статистика</b>\n"
        f"В БД всего: {db_stats.get('total', 0)}\n"
        f"По типам: {by_type}\n"
        f"Runtime signals: {snap['signals_total']}\n"
        f"Errors: {snap['errors_total']}\n"
        f"WS reconnects: {snap['ws_reconnects']}"
    )


@router.callback_query(F.data.startswith("toggle:"))
async def on_toggle(query: CallbackQuery, state: AppState) -> None:
    assert query.data is not None and query.from_user is not None
    field = query.data.split(":", 1)[1]
    settings = state.signal_engine.settings.model_copy(deep=True)
    if not hasattr(settings, field):
        await query.answer("Неизвестная настройка", show_alert=True)
        return
    setattr(settings, field, not bool(getattr(settings, field)))
    await state.apply_settings(settings, chat_id=query.from_user.id)
    if query.message:
        await query.message.edit_text(
            _settings_text(settings),
            reply_markup=settings_keyboard(settings),
        )
    await query.answer("Обновлено")


@router.callback_query(F.data.startswith("adj:"))
async def on_adj(query: CallbackQuery, state: AppState) -> None:
    assert query.data is not None and query.from_user is not None
    _, kind, delta_s = query.data.split(":")
    delta = float(delta_s)
    settings = state.signal_engine.settings.model_copy(deep=True)
    if kind == "long":
        settings.long_threshold_pct = max(0.1, round(settings.long_threshold_pct + delta, 2))
    elif kind == "short":
        settings.short_threshold_pct = max(0.1, round(settings.short_threshold_pct + delta, 2))
    elif kind == "dump":
        settings.dump_threshold_pct = max(0.1, round(settings.dump_threshold_pct + delta, 2))
    else:
        await query.answer("Неизвестная настройка", show_alert=True)
        return
    await state.apply_settings(settings, chat_id=query.from_user.id)
    if query.message:
        await query.message.edit_text(
            _settings_text(settings),
            reply_markup=settings_keyboard(settings),
        )
    await query.answer("Обновлено")
