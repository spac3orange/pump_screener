import pytest
from aiogram.types import Chat, Message, Update, User
from pydantic import ValidationError

from screener.config import ScreenerSettings
from screener.infrastructure.telegram.keyboards import settings_keyboard
from screener.infrastructure.telegram.middleware import _command_user_id
from screener.infrastructure.telegram.settings_input import (
    SettingInputError,
    apply_setting_text,
    parse_percent,
    parse_period_minutes,
    prompt_for,
    toggle_rule,
)


def test_toggle_rule_flips_only_that_rule() -> None:
    updated = toggle_rule(ScreenerSettings(), "dump")
    assert updated.enable_dump is False
    assert updated.enable_long is True and updated.enable_short is True
    with pytest.raises(SettingInputError):
        toggle_rule(ScreenerSettings(), "volume")


def test_settings_keyboard_has_toggles_and_six_buttons() -> None:
    markup = settings_keyboard(ScreenerSettings(enable_short=False))
    toggles = markup.inline_keyboard[0]
    assert [b.text for b in toggles] == ["Лонг ON", "Шорт OFF", "Дамп ON"]
    assert [b.callback_data for b in toggles] == ["toggle:long", "toggle:short", "toggle:dump"]
    *rule_rows, exchanges, nav = markup.inline_keyboard[1:]
    assert [b.callback_data for b in nav] == ["prof:list"]
    assert [b.callback_data for b in exchanges] == ["ex:bybit", "ex:binance", "ex:bitget"]
    buttons = [button for row in rule_rows for button in row]
    assert [button.text for button in buttons] == [
        "🟢 Период лонг",
        "➗ Процент лонг",
        "🔴 Период шорт",
        "➗ Процент шорт",
        "🔻 Период дамп",
        "➗ Процент дамп",
    ]
    assert [button.callback_data for button in buttons] == [
        "set:long:period",
        "set:long:pct",
        "set:short:period",
        "set:short:pct",
        "set:dump:period",
        "set:dump:pct",
    ]


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("1", 60), ("12", 720), ("60", 3600), (" 7 ", 420)],
)
def test_parse_period_minutes(text: str, seconds: int) -> None:
    assert parse_period_minutes(text) == seconds


@pytest.mark.parametrize("text", ["0", "61", "12.5", "abc", "", "-1"])
def test_parse_period_minutes_rejects(text: str) -> None:
    with pytest.raises(SettingInputError):
        parse_period_minutes(text)


@pytest.mark.parametrize(
    ("text", "value"),
    [("10.5", 10.5), ("10,5", 10.5), ("0.1", 0.1), ("500", 500.0), ("40", 40.0)],
)
def test_parse_percent(text: str, value: float) -> None:
    assert parse_percent(text) == value


@pytest.mark.parametrize("text", ["0", "-1", "501", "abc", "", "nan", "inf"])
def test_parse_percent_rejects(text: str) -> None:
    with pytest.raises(SettingInputError):
        parse_percent(text)


@pytest.mark.parametrize("kind", ["short", "dump"])
def test_fall_rules_reject_impossible_percent(kind: str) -> None:
    with pytest.raises(SettingInputError):
        apply_setting_text(ScreenerSettings(), kind, "pct", "100")
    assert apply_setting_text(ScreenerSettings(), "long", "pct", "150").long_threshold_pct == 150


def test_apply_setting_text_updates_only_the_chosen_field() -> None:
    settings = ScreenerSettings()
    updated = apply_setting_text(settings, "short", "period", "25")
    assert updated.short_interval_sec == 1500
    assert updated.long_interval_sec == settings.long_interval_sec
    assert updated.short_threshold_pct == settings.short_threshold_pct

    updated = apply_setting_text(settings, "dump", "pct", "15.5")
    assert updated.dump_threshold_pct == 15.5
    assert updated.dump_interval_sec == settings.dump_interval_sec


def test_prompt_shows_current_values() -> None:
    settings = ScreenerSettings()
    assert prompt_for("long", "period", settings) == (
        "Введите период лонг в минутах (1–60). Сейчас: 12."
    )
    assert prompt_for("short", "pct", settings) == "Введите процент шорт (до 99). Сейчас: 10."


def test_rule_interval_accepts_any_whole_minute() -> None:
    settings = ScreenerSettings(
        long_interval_sec=420, short_interval_sec=60, dump_interval_sec=3600
    )
    assert settings.long_interval_sec == 420
    assert settings.short_interval_sec == 60
    assert settings.dump_interval_sec == 3600


@pytest.mark.parametrize("seconds", [0, 30, 90, 3660])
def test_rule_interval_rejects_non_minute_or_out_of_range(seconds: int) -> None:
    with pytest.raises(ValidationError):
        ScreenerSettings(long_interval_sec=seconds)


def test_command_update_user_id() -> None:
    user = User(id=77, is_bot=False, first_name="admin")
    message = Message(
        message_id=1,
        date=0,
        chat=Chat(id=77, type="private"),
        from_user=user,
        text="/settings",
    )
    update = Update(update_id=1, message=message)
    assert _command_user_id(update) == 77
    assert _command_user_id(message) == 77
    plain = message.model_copy(update={"text": "12"})
    assert _command_user_id(Update(update_id=2, message=plain)) is None
