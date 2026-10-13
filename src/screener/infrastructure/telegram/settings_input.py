from __future__ import annotations

from screener.config import ScreenerSettings

RULES = ("long", "short", "dump")
FIELDS = ("period", "pct")

_NAMES = {
    "long": "лонг",
    "short": "шорт",
    "dump": "дамп",
}
_PERIOD_ATTR = {
    "long": "long_interval_sec",
    "short": "short_interval_sec",
    "dump": "dump_interval_sec",
}
_PCT_ATTR = {
    "long": "long_threshold_pct",
    "short": "short_threshold_pct",
    "dump": "dump_threshold_pct",
}
_ENABLE_ATTR = {
    "long": "enable_long",
    "short": "enable_short",
    "dump": "enable_dump",
}


class SettingInputError(ValueError):
    """Admin sent a period or percent the screener cannot store."""


def prompt_for(kind: str, field_name: str, settings: ScreenerSettings) -> str:
    name = _NAMES[kind]
    if field_name == "period":
        minutes = getattr(settings, _PERIOD_ATTR[kind]) // 60
        return f"Введите период {name} в минутах (1–60). Сейчас: {minutes}."
    current = getattr(settings, _PCT_ATTR[kind])
    return f"Введите процент {name}. Сейчас: {current:g}."


def parse_period_minutes(text: str) -> int:
    raw = text.strip()
    if not raw.isdigit():
        raise SettingInputError("Нужно целое число минут от 1 до 60.")
    minutes = int(raw)
    if minutes < 1 or minutes > 60:
        raise SettingInputError("Нужно целое число минут от 1 до 60.")
    return minutes * 60


def parse_percent(text: str) -> float:
    raw = text.strip().replace(",", ".")
    try:
        value = float(raw)
    except ValueError:
        raise SettingInputError("Нужно число больше 0 и не больше 500.") from None
    if value <= 0 or value > 500:
        raise SettingInputError("Нужно число больше 0 и не больше 500.")
    return round(value, 2)


def toggle_rule(settings: ScreenerSettings, kind: str) -> ScreenerSettings:
    if kind not in _ENABLE_ATTR:
        raise SettingInputError("Неизвестное правило.")
    updated = settings.model_copy(deep=True)
    attr = _ENABLE_ATTR[kind]
    setattr(updated, attr, not getattr(updated, attr))
    return updated


def apply_setting_text(
    settings: ScreenerSettings,
    kind: str,
    field_name: str,
    text: str,
) -> ScreenerSettings:
    updated = settings.model_copy(deep=True)
    if field_name == "period":
        setattr(updated, _PERIOD_ATTR[kind], parse_period_minutes(text))
    elif field_name == "pct":
        setattr(updated, _PCT_ATTR[kind], parse_percent(text))
    else:
        raise SettingInputError("Неизвестная настройка.")
    return updated
