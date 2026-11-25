from __future__ import annotations

from pydantic import BaseModel

from screener.config import MAX_RULE_INTERVAL_SEC, ScreenerSettings, normalize_symbol

RULES = ("long", "dump")
FIELDS = ("period", "pct")
ANTISPAM_FIELDS = ("cooldown", "escalate", "reset_ratio", "reset_hold")

_NAMES = {
    "long": "лонг",
    "dump": "дамп",
}
_PERIOD_ATTR = {
    "long": "long_interval_sec",
    "dump": "dump_interval_sec",
}
_PCT_ATTR = {
    "long": "long_threshold_pct",
    "dump": "dump_threshold_pct",
}
_ENABLE_ATTR = {
    "long": "enable_long",
    "dump": "enable_dump",
}
_ANTISPAM_ATTR = {
    "cooldown": "cooldown_sec",
    "escalate": "escalate_step_pct",
    "reset_ratio": "reset_ratio",
    "reset_hold": "reset_hold_sec",
}


class SettingInputError(ValueError):
    """Admin sent a period or percent the screener cannot store."""


def prompt_for(kind: str, field_name: str, settings: BaseModel) -> str:
    name = _NAMES[kind]
    if field_name == "period":
        minutes = getattr(settings, _PERIOD_ATTR[kind]) // 60
        return (
            f"Введите период {name} в минутах (целое ≥ 1, максимум "
            f"{MAX_RULE_INTERVAL_SEC // 60}). Сейчас: {minutes}."
        )
    current = getattr(settings, _PCT_ATTR[kind])
    return f"Введите процент {name} (больше 0). Сейчас: {current:g}."


def prompt_antispam(field_name: str, settings: ScreenerSettings) -> str:
    if field_name == "cooldown":
        return (
            "Введите cooldown в секундах (≥ 0) — пауза между повторными уведомлениями. "
            f"Сейчас: {settings.cooldown_sec}."
        )
    if field_name == "escalate":
        return (
            "Введите escalate в процентных пунктах (> 0) — на сколько движение должно "
            f"вырасти относительно пика для повторного сигнала. Сейчас: {settings.escalate_step_pct:g}."
        )
    if field_name == "reset_ratio":
        return (
            "Введите reset ratio (больше 0 и не больше 1) — доля порога, ниже которой "
            f"эпизод начинает сбрасываться. Сейчас: {settings.reset_ratio:g}."
        )
    if field_name == "reset_hold":
        return (
            "Введите reset hold в секундах (≥ 0) — сколько держать движение ниже "
            f"reset-порога перед сбросом эпизода. Сейчас: {settings.reset_hold_sec}."
        )
    raise SettingInputError("Неизвестная настройка.")


def parse_period_minutes(text: str) -> int:
    raw = text.strip()
    if not raw.isdigit():
        raise SettingInputError(
            f"Нужно целое число минут от 1 до {MAX_RULE_INTERVAL_SEC // 60}."
        )
    minutes = int(raw)
    if minutes < 1 or minutes > MAX_RULE_INTERVAL_SEC // 60:
        raise SettingInputError(
            f"Нужно целое число минут от 1 до {MAX_RULE_INTERVAL_SEC // 60}."
        )
    return minutes * 60


def parse_percent(text: str) -> float:
    error = "Нужно число больше 0."
    raw = text.strip().replace(",", ".")
    try:
        value = float(raw)
    except ValueError:
        raise SettingInputError(error) from None
    if not value > 0 or value != value or value == float("inf"):
        raise SettingInputError(error)
    return round(value, 2)


def parse_non_negative_int(text: str, *, label: str) -> int:
    raw = text.strip()
    if not raw.isdigit():
        raise SettingInputError(f"{label}: нужно целое число ≥ 0.")
    return int(raw)


def parse_positive_float(text: str, *, label: str) -> float:
    raw = text.strip().replace(",", ".")
    try:
        value = float(raw)
    except ValueError:
        raise SettingInputError(f"{label}: нужно число больше 0.") from None
    if not value > 0 or value != value or value == float("inf"):
        raise SettingInputError(f"{label}: нужно число больше 0.")
    return round(value, 4)


def parse_reset_ratio(text: str) -> float:
    value = parse_positive_float(text, label="Reset ratio")
    if value > 1:
        raise SettingInputError("Reset ratio: нужно число больше 0 и не больше 1.")
    return value


_TURNOVER_SUFFIX = {"k": 1_000, "к": 1_000, "m": 1_000_000, "м": 1_000_000}
TURNOVER_HINT = (
    "Введите минимальный оборот за 24ч в USDT, например 500000, 500k или 1m (0 — без фильтра)."
)
DAILY_LIMIT_HINT = (
    "Введите пару и максимум сигналов в сутки UTC, например: BTC 2 или BTCUSDT 3."
)


def antispam_help_text(settings: ScreenerSettings) -> str:
    """Detailed admin help for cooldown / escalate / reset (HTML)."""
    cooldown = settings.cooldown_sec
    escalate = settings.escalate_step_pct
    ratio = settings.reset_ratio
    hold = settings.reset_hold_sec
    reset_pct = ratio * 100
    return (
        "<b>Anti-spam — как это работает</b>\n\n"
        "После первого сигнала по монете и правилу бот не шлёт каждое мелкое "
        "колебание. Параметры ниже задают паузу, условие усиления и сброс «эпизода». "
        "Меняются в /settings и сохраняются в активный профиль.\n\n"
        f"<b>Сейчас в профиле</b>\n"
        f"• Cooldown: <code>{cooldown}</code> с\n"
        f"• Escalate: <code>+{escalate:g}</code> п.п.\n"
        f"• Reset ratio: <code>{ratio:g}</code> "
        f"(≈ {reset_pct:g}% от порога правила)\n"
        f"• Reset hold: <code>{hold}</code> с\n\n"
        "<b>Cooldown</b>\n"
        "Минимальная пауза между повторными уведомлениями по той же монете, "
        "типу сигнала и периоду. Пока пауза не прошла — повтор не отправится, "
        f"даже если движение усилилось.\n"
        f"Пример: cooldown = {cooldown} с → не раньше чем через "
        f"{cooldown // 60} мин {cooldown % 60} с после прошлого алерта.\n\n"
        "<b>Escalate</b>\n"
        "Повтор только если |изменение| выросло минимум на N процентных пунктов "
        "относительно пика с прошлого уведомления "
        f"(сейчас N = {escalate:g}).\n"
        f"Пример: был сигнал на +10%, escalate = {escalate:g} → следующий не раньше "
        f"чем при ≈ +{10 + escalate:g}% (и после cooldown).\n\n"
        "<b>Reset ratio</b>\n"
        "Доля порога правила, ниже которой движение считается «ослабшим». "
        f"При пороге Лонг 10% и ratio {ratio:g} сброс начинается, когда рост "
        f"упал ниже ≈ {10 * ratio:g}%.\n\n"
        "<b>Reset hold</b>\n"
        "Сколько секунд движение должно продержаться ниже reset-порога, чтобы "
        "эпизод закрылся. После сброса следующий пробой порога снова считается "
        f"первым сигналом (сейчас hold = {hold} с).\n\n"
        "<b>Типичный цикл</b>\n"
        "1) Цена пробила порог → первый сигнал.\n"
        "2) Пока эпизод активен: молчание, пока не прошли cooldown и escalate.\n"
        "3) Движение ослабло ниже reset-порога и держится reset hold → эпизод сброшен.\n"
        "4) Новый пробой порога → снова «первый» сигнал.\n\n"
        "<i>Anti-spam не отменяет порог Лонг/Дамп и фильтр оборота — он только "
        "режет повторы одного и того же импульса.</i>"
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


def parse_daily_limit(text: str) -> tuple[str, int]:
    parts = text.strip().split()
    if len(parts) != 2:
        raise SettingInputError(DAILY_LIMIT_HINT)
    symbol = normalize_symbol(parts[0])
    if not symbol or not symbol.endswith("USDT") or len(symbol) < 5:
        raise SettingInputError("Некорректная пара. Пример: BTC или BTCUSDT.")
    if not parts[1].isdigit():
        raise SettingInputError("Максимум сигналов — целое число от 1.")
    limit = int(parts[1])
    if limit < 1 or limit > 10_000:
        raise SettingInputError("Максимум сигналов — целое число от 1 до 10000.")
    return symbol, limit


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
        setattr(updated, _PCT_ATTR[kind], parse_percent(text))
    else:
        raise SettingInputError("Неизвестная настройка.")
    return updated


def apply_antispam_text(settings: ScreenerSettings, field_name: str, text: str) -> ScreenerSettings:
    if field_name == "cooldown":
        value: int | float = parse_non_negative_int(text, label="Cooldown")
    elif field_name == "escalate":
        value = parse_positive_float(text, label="Escalate")
    elif field_name == "reset_ratio":
        value = parse_reset_ratio(text)
    elif field_name == "reset_hold":
        value = parse_non_negative_int(text, label="Reset hold")
    else:
        raise SettingInputError("Неизвестная настройка.")
    return settings.model_copy(update={_ANTISPAM_ATTR[field_name]: value})


def apply_daily_limit(settings: ScreenerSettings, text: str) -> ScreenerSettings:
    symbol, limit = parse_daily_limit(text)
    limits = dict(settings.symbol_daily_limits)
    limits[symbol] = limit
    return settings.model_copy(update={"symbol_daily_limits": limits})


def remove_daily_limit(settings: ScreenerSettings, symbol: str) -> ScreenerSettings:
    key = normalize_symbol(symbol)
    limits = dict(settings.symbol_daily_limits)
    limits.pop(key, None)
    return settings.model_copy(update={"symbol_daily_limits": limits})
