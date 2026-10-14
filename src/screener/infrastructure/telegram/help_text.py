from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape

from screener.config import ScreenerSettings
from screener.domain.enums import Direction, HistoryQuality, SignalType
from screener.domain.models import MarketSignal, RuleMatch
from screener.domain.rules import DIRECTION_OF, SEVERITY
from screener.infrastructure.telegram.formatter import format_signal_message

TELEGRAM_MESSAGE_LIMIT = 4096
_SAMPLE_SYMBOL = "ABCUSDT"
_NBSP = "\u00a0"


@dataclass(frozen=True, slots=True)
class _RuleInfo:
    signal_type: SignalType
    enable_attr: str
    threshold_attr: str
    interval_attr: str
    emoji: str
    title: str
    settings_name: str


_RULES = (
    _RuleInfo(
        SignalType.PUMP, "enable_long", "long_threshold_pct", "long_interval_sec",
        "🟢", "рост цены", "Лонг",
    ),
    _RuleInfo(
        SignalType.DROP, "enable_short", "short_threshold_pct", "short_interval_sec",
        "🔴", "снижение цены", "Шорт",
    ),
    _RuleInfo(
        SignalType.DUMP, "enable_dump", "dump_threshold_pct", "dump_interval_sec",
        "🔻", "экстремальное падение", "Дамп",
    ),
)
_BY_TYPE = {rule.signal_type: rule for rule in _RULES}


@dataclass(frozen=True, slots=True)
class _Rule:
    info: _RuleInfo
    enabled: bool
    threshold_pct: float
    interval_sec: int

    @property
    def sign(self) -> str:
        return "+" if DIRECTION_OF[self.info.signal_type] is Direction.UP else "−"

    @property
    def minutes(self) -> str:
        return _minutes(self.interval_sec)


def _rule(settings: ScreenerSettings, signal_type: SignalType) -> _Rule:
    info = _BY_TYPE[signal_type]
    return _Rule(
        info=info,
        enabled=bool(getattr(settings, info.enable_attr)),
        threshold_pct=float(getattr(settings, info.threshold_attr)),
        interval_sec=int(getattr(settings, info.interval_attr)),
    )


def _pct(value: float) -> str:
    return escape(f"{value:g}")


def _minutes(seconds: int) -> str:
    return f"{seconds // 60}{_NBSP}мин"


def _duration(seconds: int) -> str:
    if seconds >= 60 and seconds % 60 == 0:
        return _minutes(seconds)
    return f"{seconds}{_NBSP}с"


def _usdt(value: float) -> str:
    return "$" + f"{value:,.0f}".replace(",", _NBSP)


def _price(value: float) -> str:
    text = f"{value:.4f}".rstrip("0")
    whole, _, frac = text.partition(".")
    return f"{whole}.{frac.ljust(2, '0')}"


def _sample_pct(rule: _Rule, extra: float = 2.0) -> float:
    """A change slightly beyond the threshold; drops stay below 100%."""
    pct = rule.threshold_pct + extra
    if DIRECTION_OF[rule.info.signal_type] is Direction.DOWN:
        pct = min(pct, 99.0)
    return round(pct, 2)


def _rule_block(rule: _Rule, explanation: str, disclaimer: str) -> str:
    name = rule.info.signal_type.value
    header = (
        f"{rule.info.emoji} <b>{name} — {rule.info.title}</b> "
        f"(в настройках «{rule.info.settings_name}»)"
    )
    if rule.enabled:
        state = f"Сейчас: <b>{rule.sign}{_pct(rule.threshold_pct)}% за {rule.minutes}</b>"
    else:
        state = f"⏸ {name} («{rule.info.settings_name}»): <b>выключен</b>"
    return f"{header}\n{state}\n{explanation}\n<i>{disclaimer}</i>"


def _down_sample(settings: ScreenerSettings) -> tuple[list[RuleMatch], dict[SignalType, float]]:
    drop, dump = _rule(settings, SignalType.DROP), _rule(settings, SignalType.DUMP)
    active = [rule for rule in (dump, drop) if rule.enabled]
    if not active:
        active = [_rule(settings, SignalType.PUMP)]
    matches: list[RuleMatch] = []
    pcts: dict[SignalType, float] = {}
    price: float | None = None
    for rule in active:
        signal_type = rule.info.signal_type
        if price is None:
            pct = _sample_pct(rule)
            prev = 1.0
            factor = 1 + pct / 100 if signal_type is SignalType.PUMP else 1 - pct / 100
            price = prev * factor
        else:
            # A longer-window rule of the same move: deeper change from a higher start.
            pct = min(max(_sample_pct(rule), max(pcts.values()) + 5.0), 99.0)
            prev = price / (1 - pct / 100)
        pcts[signal_type] = pct
        sign = 1 if signal_type is SignalType.PUMP else -1
        matches.append(
            RuleMatch(
                signal_type=signal_type,
                interval_sec=rule.interval_sec,
                threshold_pct=rule.threshold_pct,
                change_pct=sign * pct,
                prev_price=round(prev, 6),
                price=round(price, 6),
                detected_ts_ms=0,
                history_quality=HistoryQuality.LIVE,
            )
        )
    return matches, pcts


def _sample_alert(settings: ScreenerSettings) -> tuple[str, dict[SignalType, float]]:
    matches, pcts = _down_sample(settings)
    matches.sort(key=lambda m: m.interval_sec)
    primary = max(matches, key=lambda m: (SEVERITY[m.signal_type], abs(m.change_pct)))
    signal = MarketSignal(
        symbol=_SAMPLE_SYMBOL,
        direction=DIRECTION_OF[primary.signal_type],
        primary=primary,
        matches=tuple(matches),
        detected_at=datetime(2026, 1, 1, tzinfo=UTC),
        rule_version="help",
    )
    return format_signal_message(signal, signals_24h=1), pcts


def _part_overview(settings: ScreenerSettings, instruments: int | None) -> str:
    pump = _rule(settings, SignalType.PUMP)
    universe = (
        f"Сейчас в мониторинге: <b>{instruments}</b> инструментов.\n" if instruments else ""
    )
    example_pct = _sample_pct(pump, extra=0.0)
    new_price = _price(1 + example_pct / 100)
    example_result = (
        "условие PUMP выполнено."
        if pump.enabled
        else "условие PUMP выполнилось бы, но правило сейчас выключено."
    )
    blocks = [
        "❓ <b>Как работают сигналы</b>\n"
        "Бот круглосуточно следит за криптовалютными USDT perpetual-фьючерсами Bybit "
        "(без TradFi, pre-launch и срочных фьючерсов) и ловит резкие изменения "
        "<code>lastPrice</code>. Список инструментов обновляется автоматически "
        "через Bybit API.\n"
        f"{universe}"
        "Бот не прогнозирует рынок и не гарантирует продолжение движения.",
        "⚙️ <b>Как считается изменение</b>\n"
        "<code>Изменение % = (текущая цена − цена в начале окна) / "
        "цена в начале окна × 100</code>\n"
        f"Пример: цена была 1.00 USDT, а через {pump.minutes} стала {new_price} USDT — "
        f"это +{_pct(example_pct)}% за {pump.minutes}, {example_result}",
        _rule_block(
            pump,
            f"Цена сейчас на {_pct(pump.threshold_pct)}% или больше выше, чем "
            f"{pump.minutes} назад. Это сильный восходящий импульс, возможен резкий "
            "рост интереса к инструменту. Движение может продолжиться, а может "
            "быстро откатиться.",
            "PUMP не означает автоматически «открывать Long». "
            "Это только факт сильного роста цены.",
        ),
    ]
    drop = _rule(settings, SignalType.DROP)
    blocks.append(
        _rule_block(
            drop,
            f"Цена сейчас на {_pct(drop.threshold_pct)}% или больше ниже, чем "
            f"{drop.minutes} назад. Это выраженное нисходящее движение; к моменту "
            "сигнала значительная часть падения уже могла пройти.",
            "DROP не означает автоматически «открывать Short».",
        )
    )
    dump = _rule(settings, SignalType.DUMP)
    blocks.append(
        _rule_block(
            dump,
            f"Падение на {_pct(dump.threshold_pct)}% или больше за {dump.minutes} — "
            "экстремальное движение за короткий промежуток. Причиной могут быть "
            "новости, ликвидации, крупные продажи или другие рыночные события.",
            "DUMP не означает автоматически, что нужно открывать Short.",
        )
    )
    return "\n\n".join(blocks)


def _part_reading(settings: ScreenerSettings) -> str:
    alert, pcts = _sample_alert(settings)
    drop, dump = _rule(settings, SignalType.DROP), _rule(settings, SignalType.DUMP)
    if drop.enabled and dump.enabled:
        combo = (
            f"Например, за {dump.minutes} цена упала на {_pct(pcts[SignalType.DUMP])}%, "
            f"а за {drop.minutes} — на {_pct(pcts[SignalType.DROP])}%. "
            "Тогда одновременно выполняются условия DROP и DUMP."
        )
    else:
        combo = (
            "Например, одно и то же падение может выполнить и DROP, и DUMP, "
            "если оба правила включены."
        )
    exclusions = ""
    if settings.exclude_symbols:
        names = ", ".join(escape(s) for s in sorted(settings.exclude_symbols))
        exclusions = f"\nИсключены из сигналов: <code>{names}</code>"
    reset_pct = _pct(settings.reset_ratio * 100)
    blocks = [
        "📨 <b>Как читать уведомление</b>\n"
        f"{alert}\n\n"
        "• Первая строка — интервал главного правила и монета "
        "(тикер — ссылка на страницу инструмента на Bybit).\n"
        "• Далее строки сработавших правил: изменение цены в %, в скобках "
        "<code>цена в начале окна-текущая цена</code>.\n"
        "• Первая цена — ориентир в начале временного окна, вторая — "
        "<code>lastPrice</code> в момент обнаружения сигнала.",
        "🔗 <b>Почему могут сработать несколько правил</b>\n"
        "Один и тот же рынок может одновременно соответствовать нескольким условиям. "
        f"{combo}\n"
        "Бот объединяет связанные сигналы одного движения в одно уведомление: "
        "главная строка — самое сильное правило, ниже — остальные интервалы. "
        "Рост и падение не объединяются, а срабатывание в другой момент времени "
        "придёт отдельным сообщением.",
        "📊 <b>Signal 24h</b>\n"
        "Количество уведомлений по этому инструменту за последние 24 часа, "
        "включая текущее (объединённое уведомление считается за одно).\n"
        "<code>Signal 24h: 1</code> — первое срабатывание за сутки.\n"
        "<code>Signal 24h: 7</code> — инструмент уже несколько раз попадал в скринер.\n"
        "Большое число не означает «сильнее покупать». Это может говорить о высокой "
        "волатильности, продолжающемся движении, частых откатах, низкой ликвидности "
        "или повторных пробоях порогов.",
        "🔕 <b>Без спама</b>\n"
        "Бот не шлёт уведомление на каждое небольшое изменение после пробоя порога. "
        "Повтор по тому же правилу приходит не раньше чем через "
        f"{_duration(settings.cooldown_sec)} и только если движение усилилось ещё "
        f"на {_pct(settings.escalate_step_pct)} п.п. Новый цикл начинается, когда "
        f"движение ослабнет ниже {reset_pct}% порога и продержится так "
        f"{_duration(settings.reset_hold_sec)}.",
        "💧 <b>Фильтр ликвидности</b>\n"
        f"Минимальный оборот 24ч: <b>{_usdt(settings.min_turnover_24h_usdt)}</b>\n"
        "Инструменты с меньшим оборотом сигналов не дают. Это базовая защита от "
        "совсем низколиквидных инструментов, но она не гарантирует высокую "
        f"ликвидность и качество исполнения сделки.{exclusions}",
    ]
    return "\n\n".join(blocks)


def _part_limits() -> str:
    blocks = [
        "🧩 <b>Ограничения текущей версии</b>\n"
        "Сигнал строится на изменении <code>lastPrice</code> и фильтре оборота 24ч. "
        "В условие сигнала пока не входят краткосрочный объём, Open Interest, "
        "funding rate, данные о ликвидациях и дисбаланс стакана.\n"
        "После перезапуска или долгого обрыва связи история цен временно "
        "дополняется минутными свечами, поэтому цена в начале окна в это время "
        "приблизительная.\n"
        "Сигнал не является полноценной торговой рекомендацией.",
        "🧭 <b>Как использовать бот</b>\n"
        "После сигнала откройте график инструмента и дополнительно проверьте: "
        "структуру движения, объём, ликвидность, spread, Open Interest, новости "
        "и общий рынок.",
        "⚠️ <b>Важно</b>\n"
        "Сигналы бота — это уведомления о движении рынка, а не финансовая "
        "рекомендация. Резкие движения особенно рискованны: цена может быстро "
        "продолжить движение или так же быстро развернуться.",
        "<b>Команды</b>\n"
        "/settings — пороги, периоды и ON/OFF правил активного профиля\n"
        "/profiles — профили мониторинга: переключение, создание, копии\n"
        "/status — состояние мониторинга\n"
        "/signals — последние 10 сигналов\n"
        "/stats — статистика",
    ]
    return "\n\n".join(blocks)


def build_help_messages(
    settings: ScreenerSettings, *, instruments: int | None = None
) -> list[str]:
    """Help text rendered from the live engine settings (HTML parse mode)."""
    return [
        _part_overview(settings, instruments),
        _part_reading(settings),
        _part_limits(),
    ]
