from __future__ import annotations

from html.parser import HTMLParser
from types import SimpleNamespace

from screener.config import ScreenerSettings
from screener.infrastructure.telegram.handlers import cmd_help
from screener.infrastructure.telegram.help_text import (
    TELEGRAM_MESSAGE_LIMIT,
    build_help_messages,
)
from screener.infrastructure.telegram.settings_input import apply_setting_text, toggle_rule

NBSP = "\u00a0"
ALLOWED_TAGS = {"b", "i", "code", "pre", "a"}


def _custom() -> ScreenerSettings:
    return ScreenerSettings(
        long_threshold_pct=33,
        long_interval_sec=420,
        short_threshold_pct=11,
        short_interval_sec=1500,
        dump_threshold_pct=44,
        dump_interval_sec=1020,
        min_turnover_24h_usdt=250_000,
        cooldown_sec=120,
        escalate_step_pct=3.5,
    )


def _text(settings: ScreenerSettings) -> str:
    return "\n".join(build_help_messages(settings))


class _TagChecker(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag not in ALLOWED_TAGS:
            self.errors.append(f"unsupported tag <{tag}>")
        if self.stack and self.stack[-1] in {"code", "pre"}:
            self.errors.append(f"<{tag}> nested in <{self.stack[-1]}>")
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"unbalanced </{tag}>")
            return
        self.stack.pop()


def _assert_valid_html(part: str) -> None:
    checker = _TagChecker()
    checker.feed(part)
    checker.close()
    assert not checker.errors, checker.errors
    assert not checker.stack, checker.stack


def test_help_builds_with_defaults() -> None:
    parts = build_help_messages(ScreenerSettings(), instruments=521)
    assert len(parts) == 3
    assert all(part.strip() for part in parts)
    text = "\n".join(parts)
    assert "521" in text
    assert "Signal 24h" in text
    assert "PUMP не означает автоматически «открывать Long»" in text
    assert "DROP не означает автоматически «открывать Short»" in text


def test_current_thresholds_and_intervals_are_shown() -> None:
    text = _text(_custom())
    assert f"+33% за 7{NBSP}мин" in text
    assert f"−11% за 25{NBSP}мин" in text
    assert f"−44% за 17{NBSP}мин" in text
    assert f"через 2{NBSP}мин" in text
    assert "3.5 п.п." in text


def test_help_follows_settings_changes() -> None:
    base = ScreenerSettings()
    updated = apply_setting_text(base, "long", "pct", "27.5")
    updated = apply_setting_text(updated, "short", "period", "45")
    text = _text(updated)
    assert f"+27.5% за 12{NBSP}мин" in text
    assert f"−10% за 45{NBSP}мин" in text
    assert "+40% за" not in text


def test_disabled_rule_is_marked() -> None:
    settings = toggle_rule(ScreenerSettings(), "dump")
    text = _text(settings)
    assert "DUMP («Дамп»): <b>выключен</b>" in text
    assert f"−40% за 15{NBSP}мин" not in text
    assert "Dump:" not in text  # sample alert only uses enabled rules
    assert "Drop:" in text


def test_all_down_rules_disabled_still_renders_sample() -> None:
    settings = toggle_rule(toggle_rule(ScreenerSettings(), "dump"), "short")
    parts = build_help_messages(settings)
    assert "Pump:" in parts[1]


def test_min_turnover_is_formatted() -> None:
    assert f"${'250'}{NBSP}000" in _text(_custom())
    assert f"$100{NBSP}000" in _text(ScreenerSettings())


def test_no_hardcoded_default_values() -> None:
    text = _text(_custom())
    for stale in ("40%", f"12{NBSP}мин", f"30{NBSP}мин", f"15{NBSP}мин", "100\u00a0000", "300"):
        assert stale not in text, stale


def test_markup_is_valid_and_dynamic_values_escaped() -> None:
    settings = ScreenerSettings(exclude_symbols={"A<B&C"})
    for part in build_help_messages(settings, instruments=10):
        _assert_valid_html(part)
    joined = "\n".join(build_help_messages(settings))
    assert "A&lt;B&amp;C" in joined
    assert "A<B&C" not in joined


def test_parts_fit_telegram_limit() -> None:
    for settings in (ScreenerSettings(), _custom()):
        parts = build_help_messages(settings, instruments=999)
        assert len(parts) <= 3
        assert all(len(part) < TELEGRAM_MESSAGE_LIMIT for part in parts)


async def test_cmd_help_sends_parts_from_engine_settings() -> None:
    sent: list[str] = []

    async def answer(text: str) -> None:
        sent.append(text)

    state = SimpleNamespace(signal_engine=SimpleNamespace(settings=_custom()))
    await cmd_help(SimpleNamespace(answer=answer), state)  # type: ignore[arg-type]
    assert len(sent) == 3
    assert f"+33% за 7{NBSP}мин" in sent[0]
