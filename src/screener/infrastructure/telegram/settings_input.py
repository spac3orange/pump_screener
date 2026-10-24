from __future__ import annotations

from pydantic import BaseModel

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


def prompt_for(kind: str, field_name: str, settings: BaseModel) -> str:
    name = _NAMES[kind]
    if field_name == "period":
        minutes = getattr(settings, _PERIOD_ATTR[kind]) // 60
        return f"Введите период {name} в минутах (1–60). Сейчас: {minutes}."
    current = getattr(settings, _PCT_ATTR[kind])
    return f"Введите процент {name} (до {_MAX_PCT[kind]:g}). Сейчас: {current:g}."


def parse_period_minutes(text: str) -> int:
    raw = text.strip()
    if not raw.isdigit():
        raise SettingInputError("Нужно целое число минут от 1 до 60.")
    minutes = int(raw)
    if minutes < 1 or minutes > 60:
        raise SettingInputError("Нужно целое число минут от 1 до 60.")
    return minutes * 60


_MAX_PCT = {"long": 500.0, "short": 99.0, "dump": 99.0}


def parse_percent(text: str, max_pct: float = 500.0) -> float:
    error = f"Нужно число больше 0 и не больше {max_pct:g}."
    raw = text.strip().replace(",", ".")
    try:
        value = float(raw)
    except ValueError:
        raise SettingInputError(error) from None
    if not value > 0 or value > max_pct:
        raise SettingInputError(error)
    return round(value, 2)


_TURNOVER_SUFFIX = {"k": 1_000, "к": 1_000, "m": 1_000_000, "м": 1_000_000}
TURNOVER_HINT = (
    "Введите минимальный оборот за 24ч в USDT, например 500000, 500k или 1m (0 — без фильтра)."
)


def parse_turnover(text: str) -> float:
    raw = text.strip().lower().replace(" ", "").replace("_", "").replace(",", ".")
    raw = raw.removeprefix("$")
    mult = 1
    if raw and raw[-1] in _TURNOVER_SUFFIX:
        mult = _TURNOVER_SUFFIX[raw[-1]]
        raw = raw[:-1]
    try:
        value = float(raw) * mult
    except ValueError:
        raise SettingInputError("Нужно число USDT от 0 до 1 000 000 000.") from None
    if value < 0 or value > 1_000_000_000 or value != value:
        raise SettingInputError("Нужно число USDT от 0 до 1 000 000 000.")
    return round(value, 2)


def toggle_rule[M: BaseModel](settings: M, kind: str) -> M:
    if kind not in _ENABLE_ATTR:
        raise SettingInputError("Неизвестное правило.")
    updated = settings.model_copy(deep=True)
    attr = _ENABLE_ATTR[kind]
    setattr(updated, attr, not getattr(updated, attr))
    return updated


def apply_setting_text[M: BaseModel](
    settings: M,
    kind: str,
    field_name: str,
    text: str,
) -> M:
    updated = settings.model_copy(deep=True)
    if field_name == "period":
        setattr(updated, _PERIOD_ATTR[kind], parse_period_minutes(text))
    elif field_name == "pct":
        setattr(updated, _PCT_ATTR[kind], parse_percent(text, _MAX_PCT[kind]))
    else:
        raise SettingInputError("Неизвестная настройка.")
    return updated
