from __future__ import annotations

import html
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from screener.application.metrics import metrics
from screener.application.profiles import ProfileService
from screener.application.trading.lifecycle import PositionLifecycleService
from screener.application.trading.service import AccountStatsView, TradingService
from screener.config import ScreenerSettings
from screener.domain.exchanges import BINANCE, BITGET, BYBIT, exchanges_label, toggle_exchange
from screener.domain.profiles import ProfileError, ProfileNameTaken, normalize_profile_name
from screener.infrastructure.telegram.commercial_texts import started_users_messages
from screener.infrastructure.telegram.keyboards import (
    settings_keyboard,
    symbol_limits_keyboard,
    trading_keyboard,
    trading_mainnet_confirm_keyboard,
)
from screener.infrastructure.telegram.middleware import AdminFilter
from screener.infrastructure.telegram.profiles_view import (
    TRADING_SWITCH_WARNING,
    confirm_keyboard,
    profile_card_keyboard,
    profile_card_text,
    profiles_list_keyboard,
    profiles_list_text,
    usd_short,
)
from screener.infrastructure.telegram.settings_input import (
    DAILY_LIMIT_HINT,
    FIELDS,
    RULES,
    TURNOVER_HINT,
    SettingInputError,
    apply_daily_limit,
    apply_setting_text,
    parse_turnover,
    prompt_for,
    remove_daily_limit,
    toggle_rule,
)
from screener.infrastructure.telegram.trade_lifecycle_format import format_money
from screener.infrastructure.telegram.user_keyboards import USER_SETTINGS

if TYPE_CHECKING:
    from screener.app_state import AppState

router = Router(name="admin")
router.message.filter(AdminFilter())
router.callback_query.filter(AdminFilter())


def _money(value: object) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "$0.00"


_REASON_SHORT = {
    "TAKE_PROFIT": "TP",
    "STOP_LOSS": "SL",
    "MANUAL": "MANUAL",
    "EXTERNAL_ORDER": "EXTERNAL",
    "LIQUIDATION": "LIQUIDATION",
    "ADL": "ADL",
    "UNKNOWN": "UNKNOWN",
}


def _lifecycle_text(lifecycle: PositionLifecycleService | None) -> str:
    if lifecycle is None:
        return "Lifecycle monitor: ❌ Off\n"
    status = lifecycle.status()
    if not status.running:
        monitor = "❌ Not running"
    elif status.stream_connected:
        monitor = "✅ Connected"
    else:
        monitor = "⚠️ WS reconnecting (REST fallback)"
    text = f"Lifecycle monitor: {monitor}\n"
    last = status.last_closed
    if last is not None:
        pnl = last.net_pnl if last.net_pnl is not None else last.bybit_closed_pnl
        text += (
            "\nПоследняя закрытая сделка:\n"
            f"{html.escape(last.symbol)} {last.direction}\n"
            f"{_REASON_SHORT.get(last.close_reason.value, last.close_reason.value)}\n"
            f"{format_money(pnl) if pnl is not None else '—'}\n"
        )
    return text


def _trading_text(
    stats: AccountStatsView, lifecycle: PositionLifecycleService | None = None
) -> str:
    on = "🟢 ON" if stats.trading_enabled else "🔴 OFF"
    if not stats.trading_enabled and stats.disabled_reason:
        on += f"\nПричина OFF: {html.escape(stats.disabled_reason)}"
    api = (
        "✅ Ready"
        if stats.trading_ready
        else f"❌ Not ready ({html.escape(stats.ready_reason)})"
    )
    started = (
        f"Запущен: {stats.started_at:%Y-%m-%d %H:%M:%S} UTC\n"
        if stats.started_at is not None
        else ""
    )
    pnl = float(stats.unrealised_pnl)
    pnl_s = f"+{_money(pnl)[1:]}" if pnl >= 0 else f"-{_money(abs(pnl))[1:]}"
    return (
        "<b>Bybit Trading</b>\n"
        f"Mode: {stats.mode.upper()}\n"
        f"Trading: {on}\n\n"
        f"Equity: {_money(stats.equity)}\n"
        f"Wallet: {_money(stats.wallet)}\n"
        f"Available: {_money(stats.available)}\n"
        f"Unrealized PnL: {pnl_s}\n\n"
        f"Open positions: {stats.open_positions}\n"
        f"Active orders: {stats.active_orders}\n\n"
        f"API: {api}\n"
        f"{_lifecycle_text(lifecycle)}"
        f"{started}\n"
        "<i>OFF запрещает только новые входы. Открытые позиции и TP/SL не трогает.</i>"
    )


async def _render_trading(message: Message, state: AppState, *, force: bool = False) -> None:
    trading = state.trading
    if trading is None:
        await message.answer(
            "Торговый модуль выключен на сервере "
            "(<code>BYBIT_TRADING_MODE=disabled</code>)."
        )
        return
    stats = await trading.get_account_stats(force=force)
    await message.answer(
        _trading_text(stats, state.lifecycle),
        reply_markup=trading_keyboard(
            enabled=stats.trading_enabled,
            mainnet=trading.settings.bybit_trading_mode == "mainnet",
        ),
    )


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


def _settings_text(settings: ScreenerSettings, profile_name: str | None = None) -> str:
    lines = []
    if profile_name:
        lines.append(f"📁 Активный профиль: <b>{html.escape(profile_name)}</b>")
    lines += [
        "<b>Текущие настройки бота:</b>",
        _rule_line(
            "🟢",
            "Лонг",
            settings.long_threshold_pct,
            settings.long_interval_sec,
            settings.enable_long,
        ),
        _rule_line(
            "🔻",
            "Дамп",
            settings.dump_threshold_pct,
            settings.dump_interval_sec,
            settings.enable_dump,
            signed=True,
        ),
        f"💧 Мин. оборот 24ч: {usd_short(settings.min_turnover_24h_usdt)}",
    ]
    if settings.symbol_daily_limits:
        caps = ", ".join(
            f"{symbol}≤{settings.symbol_daily_limits[symbol]}"
            for symbol in sorted(settings.symbol_daily_limits)
        )
        lines.append(f"Лимиты/сутки UTC: {caps}")
    if settings.exclude_symbols:
        lines.append(f"Исключения: {', '.join(sorted(settings.exclude_symbols))}")
    if profile_name:
        lines.append("<i>Изменения сохраняются в активный профиль.</i>")
    return "\n".join(lines)


def _feed_available(state: AppState, code: str) -> bool:
    return code in getattr(state, "feeds", {})


def _settings_screen(state: AppState) -> tuple[str, InlineKeyboardMarkup]:
    settings = state.signal_engine.settings
    name = state.active_profile_name if state.profiles is not None else None
    chosen = frozenset(state.admin_exchanges)
    text = _settings_text(settings, name)
    text += f"\nБиржи: {exchanges_label(chosen)}"
    if BYBIT not in chosen:
        text += "\n<i>Bybit OFF: уведомления админу выключены, движок и торговля работают.</i>"
    return text, settings_keyboard(
        settings,
        chosen,
        binance_available=_feed_available(state, BINANCE),
        bitget_available=_feed_available(state, BITGET),
    )


def _running_exchanges(state: AppState) -> str:
    feeds = frozenset(getattr(state, "feeds", {}))
    return exchanges_label(feeds) if feeds else "Bybit"


@router.message(CommandStart())
async def cmd_start(message: Message, state: AppState) -> None:
    await message.answer(
        "<b>Bybit Market Screener</b>\n"
        f"Мониторинг crypto USDT Linear Perpetual на {_running_exchanges(state)}: "
        "лонг и дамп.\n\n"
        "Команды:\n"
        "/status — состояние\n"
        "/settings — настройки активного профиля\n"
        "/profiles — профили мониторинга\n"
        "/signals — последние сигналы\n"
        "/stats — статистика"
    )


@router.message(Command("trading"))
async def cmd_trading(message: Message, state: AppState) -> None:
    await _render_trading(message, state, force=True)


def _feeds_status(snap: dict) -> str:
    feeds = snap.get("feeds") or {}
    if not feeds:
        return ""
    lines = []
    for name, data in feeds.items():
        lines.append(
            f"{name}: instr={data.get('instruments_active', 0)} "
            f"ws={data.get('ws_connections', 0)} "
            f"age_ms={data.get('last_message_age_ms')} ready={data.get('ready')}"
        )
    return "Feeds:\n" + "\n".join(lines) + "\n"


@router.message(Command("status"))
async def cmd_status(message: Message, state: AppState) -> None:
    snap = metrics.snapshot()
    admins = ", ".join(str(i) for i in state.system.admin_id) or "—"
    profile = ""
    if state.profiles is not None:
        active = state.profiles.active
        version = f" v{active.profile.current.version_no}" if active.profile else ""
        profile = f"Monitoring profile: <b>{html.escape(active.name)}</b>{version}\n"
    await message.answer(
        "<b>Статус</b>\n"
        f"{profile}"
        f"Ready: {snap['ready']}\n"
        f"Bybit instruments: {snap['instruments_active']}\n"
        f"Bybit WS connections: {snap['ws_connections']}\n"
        f"Msg/s: {snap['messages_per_sec']}\n"
        f"Live price/s: {snap['rates'].get('live_price_events_per_sec', 0.0)}\n"
        f"Signals (runtime): {snap['signals_total']}\n"
        f"Raw drops: {snap['raw_protocol_drops_total']}, lost price events: "
        f"{snap['lost_price_events']}\n"
        f"Reconnects: {snap['ws_reconnects']}, gaps: {snap['history_gaps_total']}\n"
        f"p95 ms: {snap['pipeline_latency_ms']['p95']}\n"
        f"Last msg age ms: {snap['last_message_age_ms']}\n"
        f"{_feeds_status(snap)}"
        f"Admin IDs: <code>{admins}</code>"
    )


@router.message(Command("settings"))
async def cmd_settings(message: Message, state: AppState) -> None:
    text, markup = _settings_screen(state)
    await message.answer(text, reply_markup=markup)


@router.message(Command("signals"))
async def cmd_signals(message: Message, state: AppState) -> None:
    rows = await state.repo.recent_signals(limit=10)
    if not rows:
        await message.answer("Сигналов пока нет.")
        return
    lines = [
        f"{row.detected_at.strftime('%H:%M:%S')} <b>{row.signal_type}</b> "
        f"{getattr(row, 'exchange', 'bybit')} {row.symbol} {row.change_pct:+.2f}% / "
        f"{row.interval_sec // 60}m"
        f"{_signal_profile_suffix(row.payload)}"
        for row in rows
    ]
    await message.answer("<b>Последние сигналы</b>\n" + "\n".join(lines))


def _signal_profile_suffix(payload: object) -> str:
    profile = payload.get("monitoring_profile") if isinstance(payload, dict) else None
    if not isinstance(profile, dict) or not profile.get("name"):
        return ""
    version = f" v{profile['version_no']}" if profile.get("version_no") else ""
    return f" · {html.escape(str(profile['name']))}{version}"


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


@router.message(Command("admin_stats"))
async def cmd_admin_stats(message: Message, state: AppState) -> None:
    rt = state.commercial
    if rt is None:
        await message.answer("Коммерческий режим выключен (<code>COMMERCIAL_ENABLED=false</code>).")
        return
    s = await rt.repo.admin_stats(datetime.now(UTC))
    snap = metrics.snapshot()
    deliveries = snap.get("user_deliveries_total", {})
    await message.answer(
        "<b>PumpScreener — подписчики</b>\n"
        f"Пользователей: {s['users_total']} (бот заблокирован: {s['bot_blocked']})\n"
        f"Триалов всего: {s['trials_used']}, активных: {s['trials_active']}\n"
        f"Подписок активных: {s['subscriptions_active']}, "
        f"с отменённым автопродлением: {s['subscriptions_canceled']}\n"
        f"Платящих всего: {s['paid_users']}, из триала: {s['trial_converted']}\n"
        f"Выручка 30д: {s['revenue_stars_30d']} ⭐\n"
        f"Аномалий оплат 30д: {s['payment_anomalies_30d']}\n\n"
        f"Получают сигналы сейчас: {rt.registry.user_count} "
        f"в {rt.registry.group_count} группах\n"
        f"Очередь доставки: {rt.dispatcher.queued_messages}, "
        f"отправлено сообщений: {snap['user_messages_sent_total']}\n"
        f"Доставки: {', '.join(f'{k}={v}' for k, v in sorted(deliveries.items())) or '—'}\n"
        f"Flood waits: {snap['user_flood_waits_total']}, "
        f"writer backlog: {rt.writer.backlog}\n"
        f"{_feeds_status(snap)}"
    )


_NAME_LOOKUPS = 30


@router.message(Command("admin_users"))
async def cmd_admin_users(message: Message, state: AppState) -> None:
    rt = state.commercial
    if rt is None:
        await message.answer("Коммерческий режим выключен (<code>COMMERCIAL_ENABLED=false</code>).")
        return
    users = await rt.repo.list_started_users()
    looked = 0
    filled = False
    for user in users:
        if user.username or user.display_name or looked >= _NAME_LOOKUPS:
            continue
        looked += 1
        try:
            chat = await message.bot.get_chat(user.telegram_user_id)
        except Exception:
            continue
        username = getattr(chat, "username", None)
        first = getattr(chat, "first_name", None) or ""
        last = getattr(chat, "last_name", None) or ""
        display = " ".join(part for part in (str(first), str(last)) if part).strip() or None
        if not username and not display:
            continue
        await rt.repo.set_public_name(
            user.telegram_user_id, username=username, display_name=display
        )
        filled = True
    if filled:
        users = await rt.repo.list_started_users()
    for part in started_users_messages(users):
        await message.answer(part)


@router.message(Command("admin_refund"))
async def cmd_admin_refund(message: Message, state: AppState, command: CommandObject) -> None:
    rt = state.commercial
    if rt is None:
        await message.answer("Коммерческий режим выключен.")
        return
    charge_id = (command.args or "").strip()
    if not charge_id or len(charge_id) > 128 or " " in charge_id:
        await message.answer(
            "Использование: <code>/admin_refund &lt;telegram_payment_charge_id&gt;</code>"
        )
        return
    await message.answer(html.escape(await rt.payments.admin_refund(charge_id)))


async def _edit_trading_panel(
    query: CallbackQuery,
    trading: TradingService,
    *,
    force: bool = False,
    lifecycle: PositionLifecycleService | None = None,
) -> None:
    stats = await trading.get_account_stats(force=force)
    if query.message is not None:
        await query.message.edit_text(
            _trading_text(stats, lifecycle),
            reply_markup=trading_keyboard(
                enabled=stats.trading_enabled,
                mainnet=trading.settings.bybit_trading_mode == "mainnet",
            ),
        )


@router.callback_query(F.data.startswith("trading:"))
async def on_trading_action(query: CallbackQuery, state: AppState) -> None:
    assert query.data is not None and query.from_user is not None
    trading = state.trading
    if trading is None:
        await query.answer("Торговый модуль выключен", show_alert=True)
        return
    action = query.data.split(":", 1)[1]
    chat_id = query.from_user.id

    if action == "refresh":
        await _edit_trading_panel(query, trading, force=True, lifecycle=state.lifecycle)
        await query.answer("Обновлено")
        return

    if action == "off":
        await trading.set_enabled(False, chat_id=chat_id)
        await _edit_trading_panel(query, trading, force=True, lifecycle=state.lifecycle)
        await query.answer("Торговля выключена")
        return

    if action == "on" and trading.settings.bybit_trading_mode == "mainnet":
        # Only the explicit confirmation button may turn MAINNET on.
        action = "enable_ask"

    if action == "on":
        err = await trading.set_enabled(True, chat_id=chat_id)
        if err:
            await query.answer(f"Не удалось: {err}"[:190], show_alert=True)
            return
        await _edit_trading_panel(query, trading, force=True, lifecycle=state.lifecycle)
        await query.answer("Торговля включена")
        return

    if action == "enable_ask":
        trading.arm_mainnet_confirm()
        if query.message is not None:
            await query.message.answer(
                "Включить автоматическую торговлю <b>MAINNET</b>?\n"
                "Будут отправляться реальные ордера.",
                reply_markup=trading_mainnet_confirm_keyboard(),
            )
        await query.answer()
        return

    if action == "enable_cancel":
        await query.answer("Отменено")
        if query.message is not None:
            await query.message.edit_text("Включение MAINNET отменено.")
        return

    if action == "enable_confirm":
        if not trading.mainnet_confirm_armed():
            await query.answer("Подтверждение истекло. Запросите снова.", show_alert=True)
            return
        err = await trading.set_enabled(True, chat_id=chat_id)
        if err:
            await query.answer(f"Не удалось: {err}"[:190], show_alert=True)
            return
        if query.message is not None:
            await query.message.edit_text("MAINNET торговля включена.")
        await query.answer("Включено")
        return

    await query.answer("Неизвестное действие", show_alert=True)


@router.callback_query(F.data.startswith("ex:"))
async def on_toggle_exchange(query: CallbackQuery, state: AppState) -> None:
    assert query.data is not None
    code = query.data.split(":", 1)[1]
    nxt = toggle_exchange(
        frozenset(state.admin_exchanges), code, frozenset(getattr(state, "feeds", {}))
    )
    if nxt is None:
        await query.answer(
            "Нельзя выключить последнюю работающую биржу. "
            "Чтобы остановить сигналы, выключите правила в /settings.",
            show_alert=True,
        )
        return
    await state.set_admin_exchanges(set(nxt))
    if query.message is not None:
        text, markup = _settings_screen(state)
        await query.message.edit_text(text, reply_markup=markup)
    await query.answer("Обновлено")


DAILY_LIMIT_EDIT = "daily_limit"


def _limits_screen(state: AppState) -> tuple[str, InlineKeyboardMarkup]:
    settings = state.signal_engine.settings
    if settings.symbol_daily_limits:
        body = "\n".join(
            f"• <code>{html.escape(symbol)}</code> — максимум "
            f"{settings.symbol_daily_limits[symbol]} сигнал(ов) в сутки UTC"
            for symbol in sorted(settings.symbol_daily_limits)
        )
    else:
        body = "Пока нет лимитов. Добавьте пару и максимум сигналов в сутки UTC."
    text = (
        "<b>Лимиты сигналов по монетам</b>\n"
        f"{body}\n\n"
        "<i>Нажмите на пару, чтобы удалить лимит. Счётчик сбрасывается в 00:00 UTC.</i>"
    )
    return text, symbol_limits_keyboard(settings)


@router.callback_query(F.data.startswith("limits:"))
async def on_symbol_limits(query: CallbackQuery, state: AppState) -> None:
    assert query.data is not None and query.from_user is not None
    parts = query.data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    if action == "list":
        if query.message is not None:
            text, markup = _limits_screen(state)
            await query.message.edit_text(text, reply_markup=markup)
        await query.answer()
        return
    if action == "back":
        if query.message is not None:
            text, markup = _settings_screen(state)
            await query.message.edit_text(text, reply_markup=markup)
        await query.answer()
        return
    if action == "add":
        state.begin_setting_edit(query.from_user.id, DAILY_LIMIT_EDIT, "add")
        if query.message is not None:
            await query.message.answer(DAILY_LIMIT_HINT)
        await query.answer()
        return
    if action == "del" and len(parts) == 3:
        try:
            updated = remove_daily_limit(state.signal_engine.settings, parts[2])
            await state.apply_settings(updated, chat_id=query.from_user.id)
        except ProfileError as exc:
            await query.answer(str(exc)[:190], show_alert=True)
            return
        if query.message is not None:
            text, markup = _limits_screen(state)
            await query.message.edit_text(text, reply_markup=markup)
        await query.answer("Удалено")
        return
    await query.answer("Неизвестное действие", show_alert=True)


@router.callback_query(F.data.startswith("toggle:"))
async def on_toggle_rule(query: CallbackQuery, state: AppState) -> None:
    assert query.data is not None and query.from_user is not None
    kind = query.data.split(":", 1)[1]
    try:
        updated = toggle_rule(state.signal_engine.settings, kind)
    except SettingInputError:
        await query.answer("Неизвестная настройка", show_alert=True)
        return
    try:
        await state.apply_settings(updated, chat_id=query.from_user.id)
    except ProfileError as exc:
        await query.answer(str(exc)[:190], show_alert=True)
        return
    if query.message is not None:
        text, markup = _settings_screen(state)
        await query.message.edit_text(text, reply_markup=markup)
    await query.answer("Обновлено")


@router.callback_query(F.data.startswith("set:"))
async def on_set_field(query: CallbackQuery, state: AppState) -> None:
    assert query.data is not None and query.from_user is not None
    parts = query.data.split(":")
    if len(parts) == 3 and parts[1] == "turnover" and parts[2] == "value":
        state.begin_setting_edit(query.from_user.id, "turnover", "value")
        current = usd_short(state.signal_engine.settings.min_turnover_24h_usdt)
        if query.message is not None:
            await query.message.answer(f"{TURNOVER_HINT}\nСейчас: {current}.")
        await query.answer()
        return
    if len(parts) != 3 or parts[1] not in RULES or parts[2] not in FIELDS:
        await query.answer("Неизвестная настройка", show_alert=True)
        return
    _, kind, field_name = parts
    state.begin_setting_edit(query.from_user.id, kind, field_name)
    prompt = prompt_for(kind, field_name, state.signal_engine.settings)
    if query.message is not None:
        await query.message.answer(prompt)
    await query.answer()


@router.message(F.text, ~F.text.startswith("/"))
async def on_setting_value(message: Message, state: AppState) -> None:
    if message.from_user is None or not message.text:
        return
    pending = state.pending_edit(message.from_user.id)
    if pending is None:
        return
    if pending[0] == USER_SETTINGS:
        # The admin is editing their own subscriber rules; let the user router handle it.
        raise SkipHandler()
    kind, field_name = pending
    if kind == PROFILE_EDIT:
        await _on_profile_text(message, state, field_name)
        return
    if kind == DAILY_LIMIT_EDIT:
        try:
            updated = apply_daily_limit(state.signal_engine.settings, message.text)
        except SettingInputError as exc:
            await message.answer(f"{html.escape(str(exc))}\n{DAILY_LIMIT_HINT}")
            return
        state.clear_pending_edit(message.from_user.id)
        try:
            await state.apply_settings(updated, chat_id=message.from_user.id)
        except ProfileError as exc:
            await message.answer(html.escape(str(exc)))
            return
        text, markup = _limits_screen(state)
        await message.answer(text, reply_markup=markup)
        return
    if kind == "turnover":
        try:
            value = parse_turnover(message.text)
        except SettingInputError as exc:
            current = usd_short(state.signal_engine.settings.min_turnover_24h_usdt)
            await message.answer(
                f"{html.escape(str(exc))}\n{TURNOVER_HINT}\nСейчас: {current}."
            )
            return
        updated = state.signal_engine.settings.model_copy(
            update={"min_turnover_24h_usdt": value}
        )
        state.clear_pending_edit(message.from_user.id)
        try:
            await state.apply_settings(updated, chat_id=message.from_user.id)
        except ProfileError as exc:
            await message.answer(html.escape(str(exc)))
            return
        text, markup = _settings_screen(state)
        await message.answer(text, reply_markup=markup)
        return
    try:
        updated = apply_setting_text(
            state.signal_engine.settings,
            kind,
            field_name,
            message.text,
        )
    except SettingInputError as exc:
        hint = prompt_for(kind, field_name, state.signal_engine.settings)
        await message.answer(f"{html.escape(str(exc))}\n{hint}")
        return
    state.clear_pending_edit(message.from_user.id)
    try:
        await state.apply_settings(updated, chat_id=message.from_user.id)
    except ProfileError as exc:
        await message.answer(html.escape(str(exc)))
        return
    text, markup = _settings_screen(state)
    await message.answer(text, reply_markup=markup)


# --- Monitoring profiles -------------------------------------------------------

PROFILE_EDIT = "profile"


def _active_id(profiles: ProfileService) -> UUID | None:
    active = profiles.active
    return active.profile.id if active.profile else None


async def _profiles_screen(profiles: ProfileService) -> tuple[str, InlineKeyboardMarkup]:
    items = await profiles.list_profiles()
    active_id = _active_id(profiles)
    return profiles_list_text(items, active_id), profiles_list_keyboard(items, active_id)


async def _card_screen(
    profiles: ProfileService, profile_id: UUID
) -> tuple[str, InlineKeyboardMarkup]:
    profile = await profiles.get_profile(profile_id)
    active = profile.id == _active_id(profiles)
    return (
        profile_card_text(profile, active=active),
        profile_card_keyboard(profile, active=active),
    )


async def _show(query: CallbackQuery, screen: tuple[str, InlineKeyboardMarkup]) -> None:
    if isinstance(query.message, Message):
        await query.message.edit_text(screen[0], reply_markup=screen[1])


@router.message(Command("profiles"))
async def cmd_profiles(message: Message, state: AppState) -> None:
    if state.profiles is None:
        await message.answer("Профили недоступны.")
        return
    text, markup = await _profiles_screen(state.profiles)
    await message.answer(text, reply_markup=markup)


def _parse_profile_callback(data: str) -> tuple[str, UUID | None]:
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    if len(parts) == 3:
        try:
            return action, UUID(parts[2])
        except ValueError:
            return "", None
    return action, None


@router.callback_query(F.data.startswith("prof:"))
async def on_profile_action(query: CallbackQuery, state: AppState) -> None:
    assert query.data is not None and query.from_user is not None
    profiles = state.profiles
    if profiles is None:
        await query.answer("Профили недоступны", show_alert=True)
        return
    action, pid = _parse_profile_callback(query.data)
    user_id = query.from_user.id
    try:
        await _profile_action(query, state, profiles, action, pid, user_id)
    except ProfileError as exc:
        await query.answer(str(exc)[:190], show_alert=True)


async def _profile_action(
    query: CallbackQuery,
    state: AppState,
    profiles: ProfileService,
    action: str,
    pid: UUID | None,
    user_id: int,
) -> None:
    if action == "list":
        await _show(query, await _profiles_screen(profiles))
        await query.answer()
        return
    if action == "settings":
        await _show(query, _settings_screen(state))
        await query.answer()
        return
    if action == "new":
        state.begin_setting_edit(user_id, PROFILE_EDIT, "create")
        if isinstance(query.message, Message):
            await query.message.answer(
                "Введите имя нового профиля.\n"
                f"Будет сохранена копия настроек активного профиля "
                f"«{html.escape(profiles.active.name)}»."
            )
        await query.answer()
        return
    if pid is None:
        await query.answer("Неизвестное действие", show_alert=True)
        return

    if action == "open":
        await _show(query, await _card_screen(profiles, pid))
        await query.answer()
        return
    if action == "act":
        if state.trading is not None and state.trading.enabled:
            profile = await profiles.get_profile(pid)
            if isinstance(query.message, Message):
                await query.message.edit_text(
                    f"Активировать профиль <b>{html.escape(profile.name)}</b>?\n\n"
                    f"{TRADING_SWITCH_WARNING}",
                    reply_markup=confirm_keyboard(
                        "Активировать", f"prof:act_ok:{pid}", f"prof:open:{pid}"
                    ),
                )
            await query.answer()
            return
        action = "act_ok"
    if action == "act_ok":
        active = await profiles.activate(pid, chat_id=user_id)
        await _show(query, await _card_screen(profiles, pid))
        await query.answer(f"Активен: {active.name}"[:190])
        return
    if action == "clone":
        state.begin_setting_edit(user_id, PROFILE_EDIT, f"clone:{pid}")
        source = await profiles.get_profile(pid)
        if isinstance(query.message, Message):
            await query.message.answer(
                f"Введите имя копии профиля «{html.escape(source.name)}»."
            )
        await query.answer()
        return
    if action == "ren":
        state.begin_setting_edit(user_id, PROFILE_EDIT, f"rename:{pid}")
        source = await profiles.get_profile(pid)
        if isinstance(query.message, Message):
            await query.message.answer(
                f"Введите новое имя для «{html.escape(source.name)}»."
            )
        await query.answer()
        return
    if action == "del":
        profile = await profiles.get_profile(pid)
        if isinstance(query.message, Message):
            await query.message.edit_text(
                f"Удалить профиль <b>{html.escape(profile.name)}</b>?\n"
                "Уже отправленные сигналы сохранят ссылку на его версию.",
                reply_markup=confirm_keyboard(
                    "🗑 Удалить", f"prof:del_ok:{pid}", f"prof:open:{pid}"
                ),
            )
        await query.answer()
        return
    if action == "del_ok":
        await profiles.delete(pid)
        await _show(query, await _profiles_screen(profiles))
        await query.answer("Профиль удалён")
        return
    await query.answer("Неизвестное действие", show_alert=True)


async def _on_profile_text(message: Message, state: AppState, field_name: str) -> None:
    assert message.from_user is not None and message.text is not None
    profiles = state.profiles
    user_id = message.from_user.id
    if profiles is None:
        state.clear_pending_edit(user_id)
        return
    step, _, arg = field_name.partition(":")
    try:
        if step == "create":
            name = normalize_profile_name(message.text)
            if any(p.name.lower() == name.lower() for p in await profiles.list_profiles()):
                raise ProfileNameTaken(f"Профиль «{name}» уже существует.")
            state.begin_setting_edit(user_id, PROFILE_EDIT, f"create_desc:{name}")
            await message.answer("Введите описание профиля или «-», чтобы пропустить.")
            return
        if step == "create_desc":
            profile = await profiles.create_from_current(
                arg, description=message.text, chat_id=user_id
            )
            done = f"Профиль «{html.escape(profile.name)}» создан."
        elif step == "clone":
            profile = await profiles.clone(UUID(arg), message.text, chat_id=user_id)
            done = f"Создана копия «{html.escape(profile.name)}»."
        elif step == "rename":
            profile = await profiles.rename(UUID(arg), message.text)
            done = f"Профиль переименован в «{html.escape(profile.name)}»."
        else:
            state.clear_pending_edit(user_id)
            return
    except ProfileError as exc:
        await message.answer(
            f"{html.escape(str(exc))}\nПопробуйте ещё раз или отправьте /profiles."
        )
        return
    state.clear_pending_edit(user_id)
    text, markup = await _card_screen(profiles, profile.id)
    await message.answer(f"{done}\n\n{text}", reply_markup=markup)
