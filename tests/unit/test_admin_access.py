from screener.config import SystemSettings


def test_is_admin_single() -> None:
    s = SystemSettings(admin_id=[12345], telegram_bot_token="x")
    assert s.is_admin(12345)
    assert not s.is_admin(1)
    assert not s.is_admin(None)


def test_is_admin_empty_denies_all() -> None:
    s = SystemSettings(admin_id=[], telegram_bot_token="x")
    assert not s.is_admin(12345)
