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

# Telegram message limit is 4096; keep a margin for markup and page footer.
TELEGRAM_TEXT_LIMIT = 3900
PROFILES_PAGE_SIZE = 5


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


def _rules_summary_compact(settings: ScreenerSettings) -> list[str]:
    """Shorter block for the paginated list (full details stay on the card)."""
    s = settings
    lines = [
        _rule("PUMP", s.enable_long, s.long_threshold_pct, s.long_interval_sec),
        _rule("DUMP", s.enable_dump, s.dump_threshold_pct, s.dump_interval_sec, minus=True),
        f"Min turnover: {usd_short(s.min_turnover_24h_usdt)}",
    ]
    if s.symbol_daily_limits:
        lines.append(f"Daily caps: {len(s.symbol_daily_limits)}")
    return lines


def _profile_block(profile: MonitoringProfile, active_id: UUID | None) -> list[str]:
    mark = "✅" if profile.id == active_id else "•"
    lines = [f"{mark} <b>{html.escape(profile.name)}</b> (v{profile.current.version_no})"]
    settings = _settings_or_none(profile)
    if settings is None:
        lines.append("   ⚠️ некорректная конфигурация")
        return lines
    lines.extend(f"   {line}" for line in _rules_summary_compact(settings))
    return lines


def _clamp_page(page: int, total_pages: int) -> int:
    return max(0, min(page, total_pages - 1))


def _page_slice(
    profiles: list[MonitoringProfile], page: int, page_size: int
) -> tuple[list[MonitoringProfile], int, int]:
    total_pages = max(1, (len(profiles) + page_size - 1) // page_size) if profiles else 1
    page = _clamp_page(page, total_pages)
    start = page * page_size
    return profiles[start : start + page_size], page, total_pages


def profiles_list_text(
    profiles: list[MonitoringProfile],
    active_id: UUID | None,
    *,
    page: int = 0,
    page_size: int = PROFILES_PAGE_SIZE,
) -> str:
    chunk, page, total_pages = _page_slice(profiles, page, page_size)
    lines = ["📁 <b>Профили мониторинга</b>"]
    if total_pages > 1:
        lines.append(f"Страница {page + 1}/{total_pages}")
    lines.append("")
    if not chunk:
        lines.append("Профилей пока нет.")
    else:
        for p in chunk:
            lines.extend(_profile_block(p, active_id))
            lines.append("")
    lines.append("Выберите профиль, чтобы активировать, скопировать или изменить.")
    return "\n".join(lines).rstrip()


def profiles_list_page(
    profiles: list[MonitoringProfile],
    active_id: UUID | None,
    *,
    page: int = 0,
) -> tuple[str, InlineKeyboardMarkup]:
    """Build a page that fits Telegram's message length limit."""
    page_size = PROFILES_PAGE_SIZE
    while page_size >= 1:
        text = profiles_list_text(profiles, active_id, page=page, page_size=page_size)
        chunk, page, total_pages = _page_slice(profiles, page, page_size)
        if len(text) <= TELEGRAM_TEXT_LIMIT or page_size == 1:
            return text, profiles_list_keyboard(
                chunk, active_id, page=page, total_pages=total_pages
            )
        page_size -= 1
    text = profiles_list_text(profiles, active_id, page=page, page_size=1)
    chunk, page, total_pages = _page_slice(profiles, page, 1)
    return text, profiles_list_keyboard(chunk, active_id, page=page, total_pages=total_pages)


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
    profiles: list[MonitoringProfile],
    active_id: UUID | None,
    *,
    page: int = 0,
    total_pages: int = 1,
) -> InlineKeyboardMarkup:
    rows = [
        [_button(f"{'✅' if p.id == active_id else '•'} {p.name}", f"prof:open:{p.id}")]
        for p in profiles
    ]
    if total_pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(_button("◀️", f"prof:list:{page - 1}"))
        nav.append(_button(f"{page + 1}/{total_pages}", f"prof:list:{page}"))
        if page + 1 < total_pages:
            nav.append(_button("▶️", f"prof:list:{page + 1}"))
        rows.append(nav)
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
