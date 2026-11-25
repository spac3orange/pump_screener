from __future__ import annotations

import html
from uuid import UUID

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from screener.config import ScreenerSettings
from screener.domain.profiles import MonitoringProfile

TRADING_SWITCH_WARNING = (
    "⚠️ Автоторговля включена. Новый профиль изменит условия генерации "
    "торговых сигналов."
)


def usd_short(value: float) -> str:
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if value >= limit:
            return f"${value / limit:g}{suffix}"
    return f"${value:g}"


def _rule(name: str, enabled: bool, pct: float, interval_sec: int, *, minus: bool = False) -> str:
    value = f"{'-' if minus else ''}{pct:g}% / {interval_sec // 60}m"
    return f"{name}: {value}" if enabled else f"{name}: OFF ({value})"


def rules_summary(settings: ScreenerSettings) -> list[str]:
    s = settings
    lines = [
        _rule("PUMP", s.enable_long, s.long_threshold_pct, s.long_interval_sec),
        _rule("DUMP", s.enable_dump, s.dump_threshold_pct, s.dump_interval_sec, minus=True),
        f"Min turnover: {usd_short(s.min_turnover_24h_usdt)}",
    ]
    if s.symbol_daily_limits:
        caps = ", ".join(
            f"{symbol}:{s.symbol_daily_limits[symbol]}"
            for symbol in sorted(s.symbol_daily_limits)
        )
        lines.append(f"Daily caps: {caps}")
    return lines


def profiles_list_text(profiles: list[MonitoringProfile], active_id: UUID | None) -> str:
    lines = ["📁 <b>Профили мониторинга</b>", ""]
    for p in profiles:
        mark = "✅" if p.id == active_id else "•"
        lines.append(f"{mark} <b>{html.escape(p.name)}</b> (v{p.current.version_no})")
        settings = _settings_or_none(p)
        if settings is None:
            lines.append("   ⚠️ некорректная конфигурация")
            continue
        lines.extend(f"   {line}" for line in rules_summary(settings))
    lines += ["", "Выберите профиль, чтобы активировать, скопировать или изменить."]
    return "\n".join(lines)


def profile_card_text(profile: MonitoringProfile, *, active: bool) -> str:
    lines = [f"📁 <b>{html.escape(profile.name)}</b>" + (" — ✅ активный" if active else "")]
    if profile.description:
        lines.append(f"<i>{html.escape(profile.description)}</i>")
    lines.append(
        f"Версия: {profile.current.version_no} · "
        f"config <code>{profile.current.config_hash[:12]}</code>"
    )
    if profile.is_system:
        lines.append("Системный профиль (нельзя удалить)")
    settings = _settings_or_none(profile)
    if settings is None:
        lines.append("⚠️ Конфигурация не проходит проверку — активация невозможна.")
        return "\n".join(lines)
    lines.append("")
    lines.extend(rules_summary(settings))
    lines.append(
        f"Cooldown: {settings.cooldown_sec}s · Escalate: +{settings.escalate_step_pct:g}% · "
        f"Reset: {settings.reset_ratio:g} / {settings.reset_hold_sec}s"
    )
    if settings.exclude_symbols:
        lines.append(f"Исключения: {html.escape(', '.join(sorted(settings.exclude_symbols)))}")
    return "\n".join(lines)


def _settings_or_none(profile: MonitoringProfile) -> ScreenerSettings | None:
    try:
        return ScreenerSettings.from_profile_config(
            profile.current.config, profile.current.config_schema_version
        )
    except ValueError:
        return None


def _button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def profiles_list_keyboard(
    profiles: list[MonitoringProfile], active_id: UUID | None
) -> InlineKeyboardMarkup:
    rows = [
        [_button(f"{'✅' if p.id == active_id else '•'} {p.name}", f"prof:open:{p.id}")]
        for p in profiles
    ]
    rows.append(
        [_button("➕ Создать из текущих", "prof:new"), _button("⚙️ Настройки", "prof:settings")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def profile_card_keyboard(profile: MonitoringProfile, *, active: bool) -> InlineKeyboardMarkup:
    pid = profile.id
    first: list[InlineKeyboardButton] = []
    if not active:
        first.append(_button("✅ Активировать", f"prof:act:{pid}"))
    first.append(_button("📋 Копировать", f"prof:clone:{pid}"))
    second = [_button("✏️ Переименовать", f"prof:ren:{pid}")]
    if not active and not profile.is_system:
        second.append(_button("🗑 Удалить", f"prof:del:{pid}"))
    return InlineKeyboardMarkup(
        inline_keyboard=[first, second, [_button("⬅️ К списку", "prof:list")]]
    )


def confirm_keyboard(ok_text: str, ok_data: str, cancel_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[_button(ok_text, ok_data), _button("Отмена", cancel_data)]]
    )
