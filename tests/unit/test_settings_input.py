import pytest
from aiogram.types import Chat, Message, Update, User
from pydantic import ValidationError

from screener.config import ScreenerSettings, normalize_symbol
from screener.infrastructure.telegram.keyboards import settings_keyboard, symbol_limits_keyboard
from screener.infrastructure.telegram.middleware import _command_user_id
from screener.infrastructure.telegram.settings_input import (
    SettingInputError,
    apply_daily_limit,
    apply_setting_text,
    parse_daily_limit,
    parse_percent,
    parse_period_minutes,
    parse_turnover,
    prompt_for,
    remove_daily_limit,
    toggle_rule,
)


def test_toggle_rule_flips_only_that_rule() -> None:
    updated = toggle_rule(ScreenerSettings(), "dump")
    assert updated.enable_dump is False
    assert updated.enable_long is True and updated.enable_short is False
    with pytest.raises(SettingInputError):
        toggle_rule(ScreenerSettings(), "short")
    with pytest.raises(SettingInputError):
        toggle_rule(ScreenerSettings(), "volume")


def test_settings_keyboard_has_long_dump_turnover_and_limits() -> None:
    markup = settings_keyboard(ScreenerSettings(), binance_available=False, bitget_available=False)
    toggles = markup.inline_keyboard[0]
    assert [b.text for b in toggles] == ["Лонг ON", "Дамп ON"]
    assert [b.callback_data for b in toggles] == ["toggle:long", "toggle:dump"]
    *rule_rows, turnover, exchanges, limits, nav = markup.inline_keyboard[1:]
    assert [b.callback_data for b in nav] == ["prof:list"]
    assert [b.callback_data for b in exchanges] == ["ex:bybit"]
    assert [b.callback_data for b in limits] == ["limits:list"]
    assert [b.callback_data for b in turnover] == ["set:turnover:value"]
    buttons = [button for row in rule_rows for button in row]
    assert [button.text for button in buttons] == [
        "🟢 Период лонг",
        "➗ Процент лонг",
        "🔻 Период дамп",
        "➗ Процент дамп",
    ]
    assert [button.callback_data for button in buttons] == [
        "set:long:period",
        "set:long:pct",
        "set:dump:period",
        "set:dump:pct",
    ]


def test_symbol_limits_keyboard_lists_caps() -> None:
    settings = ScreenerSettings(symbol_daily_limits={"BTCUSDT": 2, "ETHUSDT": 1})
    markup = symbol_limits_keyboard(settings)
    flat = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert flat[0] == "limits:add"
    assert "limits:del:BTCUSDT" in flat
    assert "limits:del:ETHUSDT" in flat
    assert flat[-1] == "limits:back"


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


def test_dump_rejects_impossible_percent() -> None:
    with pytest.raises(SettingInputError):
        apply_setting_text(ScreenerSettings(), "dump", "pct", "100")
    assert apply_setting_text(ScreenerSettings(), "long", "pct", "150").long_threshold_pct == 150


def test_apply_setting_text_updates_only_the_chosen_field() -> None:
    settings = ScreenerSettings()
    updated = apply_setting_text(settings, "dump", "period", "25")
    assert updated.dump_interval_sec == 1500
    assert updated.long_interval_sec == settings.long_interval_sec
    assert updated.dump_threshold_pct == settings.dump_threshold_pct

    updated = apply_setting_text(settings, "dump", "pct", "15.5")
    assert updated.dump_threshold_pct == 15.5
    assert updated.dump_interval_sec == settings.dump_interval_sec


def test_prompt_shows_current_values() -> None:
    settings = ScreenerSettings()
    assert prompt_for("long", "period", settings) == (
        "Введите период лонг в минутах (1–60). Сейчас: 12."
    )
    assert prompt_for("dump", "pct", settings) == "Введите процент дамп (до 99). Сейчас: 40."


@pytest.mark.parametrize(
    ("text", "value"),
    [("500000", 500_000.0), ("500k", 500_000.0), ("1m", 1_000_000.0), ("0", 0.0)],
)
def test_parse_turnover(text: str, value: float) -> None:
    assert parse_turnover(text) == value


def test_normalize_symbol_and_daily_limit_parse() -> None:
    assert normalize_symbol("btc") == "BTCUSDT"
    assert normalize_symbol("BTCUSDT") == "BTCUSDT"
    assert parse_daily_limit("BTC 2") == ("BTCUSDT", 2)
    assert parse_daily_limit("ethusdt 3") == ("ETHUSDT", 3)
    with pytest.raises(SettingInputError):
        parse_daily_limit("BTC")
    with pytest.raises(SettingInputError):
        parse_daily_limit("BTC 0")


def test_apply_and_remove_daily_limit() -> None:
    settings = ScreenerSettings()
    updated = apply_daily_limit(settings, "btc 2")
    assert updated.symbol_daily_limits == {"BTCUSDT": 2}
    cleared = remove_daily_limit(updated, "BTCUSDT")
    assert cleared.symbol_daily_limits == {}


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
