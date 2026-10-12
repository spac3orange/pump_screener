from screener.config import ScreenerSettings, SystemSettings, get_system_settings


def test_screener_settings_defaults() -> None:
    s = ScreenerSettings()
    assert s.enable_long is True
    assert s.long_threshold_pct == 40.0
    assert s.long_interval_sec == 720
    assert s.short_threshold_pct == 10.0
    assert s.short_interval_sec == 1800
    assert s.dump_threshold_pct == 40.0
    assert s.dump_interval_sec == 900
    assert s.enable_volume_spike is False
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
