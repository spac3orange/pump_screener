import pytest
from pydantic import ValidationError

from screener.config import TradingSettings


def test_defaults_and_mapping() -> None:
    s = TradingSettings(
        bybit_trading_mode="demo",
        bybit_api_key="k",
        bybit_api_secret="s",
        _env_file=None,
    )
    assert s.action_for("PUMP") == "SHORT"
    assert s.action_for("DROP") == "IGNORE"
    assert s.action_for("DUMP") == "LONG"
    assert s.orders_environment_ok()


def test_mainnet_requires_allow_flag() -> None:
    s = TradingSettings(
        bybit_trading_mode="mainnet",
        allow_mainnet_trading=False,
        bybit_api_key="k",
        bybit_api_secret="s",
        _env_file=None,
    )
    assert not s.orders_environment_ok()
    assert not s.mainnet_orders_allowed()
    s2 = TradingSettings(
        bybit_trading_mode="mainnet",
        allow_mainnet_trading=True,
        bybit_api_key="k",
        bybit_api_secret="s",
        _env_file=None,
    )
    assert s2.orders_environment_ok()


def test_invalid_action_rejected() -> None:
    with pytest.raises(ValidationError):
        TradingSettings(pump_action="BUY", _env_file=None)
