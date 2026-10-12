from screener.application.dedup import DedupKey, SignalDeduplicator
from screener.domain.enums import SignalType


def test_first_trigger_notifies() -> None:
    d = SignalDeduplicator(cooldown_sec=300, escalate_step_pct=2.0)
    key = DedupKey("BTCUSDT", SignalType.PUMP, 600)
    dec = d.evaluate(key, change_pct=5.0, threshold_pct=5.0, now_ms=0, triggered=True)
    assert dec.should_notify
    assert dec.level == 1


def test_small_increase_suppressed() -> None:
    d = SignalDeduplicator(cooldown_sec=300, escalate_step_pct=2.0)
    key = DedupKey("BTCUSDT", SignalType.PUMP, 600)
    d.evaluate(key, change_pct=5.0, threshold_pct=5.0, now_ms=0, triggered=True)
    for pct, ts in [(5.1, 1000), (5.2, 2000), (5.3, 3000)]:
        dec = d.evaluate(key, change_pct=pct, threshold_pct=5.0, now_ms=ts, triggered=True)
        assert not dec.should_notify


def test_escalate_after_cooldown() -> None:
    d = SignalDeduplicator(cooldown_sec=10, escalate_step_pct=2.0)
    key = DedupKey("ETHUSDT", SignalType.PUMP, 600)
    d.evaluate(key, change_pct=5.0, threshold_pct=5.0, now_ms=0, triggered=True)
    dec = d.evaluate(key, change_pct=7.1, threshold_pct=5.0, now_ms=11_000, triggered=True)
    assert dec.should_notify
    assert dec.level == 2


def test_reset_after_hold() -> None:
    d = SignalDeduplicator(cooldown_sec=10, reset_ratio=0.5, reset_hold_sec=60)
    key = DedupKey("SOLUSDT", SignalType.DUMP, 600)
    d.evaluate(key, change_pct=-6.0, threshold_pct=5.0, now_ms=0, triggered=True)
    # below reset threshold 2.5
    d.evaluate(key, change_pct=-1.0, threshold_pct=5.0, now_ms=1000, triggered=False)
    dec = d.evaluate(key, change_pct=-1.0, threshold_pct=5.0, now_ms=61_000, triggered=False)
    assert dec.reason == "reset"
    # new first trigger
    dec2 = d.evaluate(key, change_pct=-5.0, threshold_pct=5.0, now_ms=62_000, triggered=True)
    assert dec2.should_notify and dec2.level == 1
