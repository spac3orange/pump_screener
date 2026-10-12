from screener.config import ScreenerSettings, SystemSettings, get_system_settings


def test_screener_settings_defaults() -> None:
    s = ScreenerSettings()
    assert s.pump_threshold_pct == 5.0
    assert 600 in s.signal_intervals_sec
    assert s.rule_version()


def test_invalid_interval_rejected() -> None:
    try:
        ScreenerSettings(signal_intervals_sec=[120])
        assert False, "expected validation error"
    except Exception:
        pass


def test_admin_id_from_env(monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_ID", "111,222")
    monkeypatch.setenv("SIGNAL_INTERVALS_SEC", "60,300")
    monkeypatch.setenv("EXCLUDE_SYMBOLS", "BTCUSDT,ethusdt")
    get_system_settings.cache_clear()
    settings = SystemSettings()
    assert settings.admin_id == [111, 222]
    assert settings.is_admin(111)
    assert not settings.is_admin(999)
    assert settings.signal_intervals_sec == [60, 300]
    assert settings.exclude_symbols == {"BTCUSDT", "ETHUSDT"}
    get_system_settings.cache_clear()


def test_admin_id_alias_telegram_allowed_ids(monkeypatch) -> None:
    monkeypatch.delenv("ADMIN_ID", raising=False)
    monkeypatch.setenv("TELEGRAM_ALLOWED_IDS", "42")
    get_system_settings.cache_clear()
    settings = SystemSettings()
    assert settings.admin_id == [42]
    get_system_settings.cache_clear()
