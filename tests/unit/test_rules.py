from screener.domain.rules import is_dump, is_pump, price_change_pct, window_tolerance_ms


def test_price_change_pct() -> None:
    assert abs(price_change_pct(105, 100) - 5.0) < 1e-9
    assert abs(price_change_pct(93, 100) - (-7.0)) < 1e-9


def test_pump_dump() -> None:
    assert is_pump(5.0, 5.0)
    assert not is_pump(4.9, 5.0)
    assert is_dump(-5.0, 5.0)
    assert not is_dump(-4.9, 5.0)


def test_window_tolerance() -> None:
    # max(2s, 5% of interval): 60s -> 3s, 3600s -> 180s
    assert window_tolerance_ms(60) == 3000
    assert window_tolerance_ms(3600) == 180_000
    assert window_tolerance_ms(30) == 2000
