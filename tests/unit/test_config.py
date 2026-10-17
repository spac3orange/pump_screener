import pytest
from pydantic import ValidationError

from screener.config import ScreenerSettings, SystemSettings, get_system_settings


def test_binance_system_defaults() -> None:
    s = SystemSettings(_env_file=None)
    assert s.binance_enabled is True
    assert s.binance_rest_base == "https://fapi.binance.com"
    assert "market/stream" in s.binance_ws_market
    assert s.binance_kline_concurrency == 4
    assert s.bitget_enabled is True
    assert s.bitget_rest_base == "https://api.bitget.com"
    assert s.bitget_ws_public == "wss://ws.bitget.com/v3/ws/public"
    assert s.bitget_symbols_per_connection == 40
    assert s.bitget_kline_concurrency == 4


def test_screener_settings_defaults() -> None:
    s = ScreenerSettings()
    assert s.enable_long is True
    assert s.long_threshold_pct == 40.0
    assert s.long_interval_sec == 720
    assert s.short_threshold_pct == 10.0
    assert s.short_interval_sec == 1800
    assert s.dump_threshold_pct == 40.0
    assert s.dump_interval_sec == 900
    assert s.allow_mixed_history is True
    assert s.correlation_window_ms == 400
    assert s.rule_version()


def test_invalid_interval_rejected() -> None:
    with pytest.raises(ValidationError):
        ScreenerSettings(long_interval_sec=120 + 30)


def test_old_stored_json_with_dead_settings_loads() -> None:
    stored = {
        "long_interval_sec": 720,
        "short_interval_sec": 1800,
        "enable_volume_spike": True,
        "signal_intervals_sec": [60, 300],
        "pump_threshold_pct": 5.0,
        "volume_ratio_threshold": 3.0,
        "min_abs_turnover_window_usdt": 1.0,
        "volume_baseline_windows": 6,
        "warmup_suppress_until_live": True,
    }
    settings = ScreenerSettings.model_validate(stored)
    dumped = settings.model_dump()
    assert "enable_volume_spike" not in dumped
    assert "signal_intervals_sec" not in dumped


def test_admin_id_from_env(monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_ID", "111,222")
    monkeypatch.setenv("EXCLUDE_SYMBOLS", "BTCUSDT,ethusdt")
    monkeypatch.setenv("SIGNAL_INTERVALS_SEC", "60,300")  # dead key is ignored
    get_system_settings.cache_clear()
    settings = SystemSettings()
    assert settings.admin_id == [111, 222]
    assert settings.is_admin(111)
    assert not settings.is_admin(999)
    assert settings.exclude_symbols == {"BTCUSDT", "ETHUSDT"}
    get_system_settings.cache_clear()


def test_admin_id_alias_telegram_allowed_ids(monkeypatch) -> None:
    monkeypatch.delenv("ADMIN_ID", raising=False)
    monkeypatch.setenv("TELEGRAM_ALLOWED_IDS", "42")
    get_system_settings.cache_clear()
    settings = SystemSettings()
    assert settings.admin_id == [42]
    get_system_settings.cache_clear()
