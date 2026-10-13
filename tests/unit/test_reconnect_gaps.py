"""Reconnect / gap regressions: short outages must not restart window warmup."""

import pytest

from screener.domain.enums import HistoryQuality
from tests.helpers import T0, Harness


def _at(sec: float) -> int:
    return T0 + int(sec * 1000)


async def _live_minutes(h: Harness, symbol: str, start_sec: int, end_sec: int) -> None:
    price = 100.0
    for s in range(start_sec, end_sec, 7):
        price = 100.0 + (s % 2) * 0.01
        await h.send(symbol, "delta", _at(s), lastPrice=str(price))
    await h.drain()


@pytest.mark.asyncio
async def test_short_disconnect_keeps_30m_window_usable() -> None:
    h = Harness()
    sym = "GAPUSDT"
    await h.send(sym, "snapshot", _at(0), lastPrice="100", turnover24h="1000000")
    await _live_minutes(h, sym, 1, 1500)  # 25 minutes of valid history

    # WS drops for ~3 seconds, then reconnects and gets a fresh snapshot.
    h.history.mark_disconnected([sym], at_ts_ms=_at(1500))
    h.processor.on_resubscribe([sym])
    await h.send(sym, "snapshot", _at(1503), lastPrice="100.5")
    await h.drain()

    # Shorter windows are usable right after reconnect, no new warmup.
    assert h.history.lookup(sym, now_ms=_at(1504), interval_sec=720) is not None
    await _live_minutes(h, sym, 1504, 1810)
    # As soon as history spans 30 minutes, the 30m window is valid.
    got = h.history.lookup(sym, now_ms=_at(1805), interval_sec=1800)
    assert got is not None
    assert got.quality is HistoryQuality.LIVE


@pytest.mark.asyncio
async def test_long_gap_invalidates_only_affected_windows_until_backfill() -> None:
    h = Harness()
    sym = "LONGGAPUSDT"
    await h.send(sym, "snapshot", _at(0), lastPrice="100", turnover24h="1000000")
    await _live_minutes(h, sym, 1, 1500)

    h.history.mark_disconnected([sym], at_ts_ms=_at(1500))
    h.processor.on_resubscribe([sym])
    await h.send(sym, "snapshot", _at(1800), lastPrice="100")  # 5 minute outage
    await _live_minutes(h, sym, 1801, 2601)

    now = _at(2600)
    # 12m window starts after the gap: valid.
    assert h.history.lookup(sym, now_ms=now, interval_sec=720) is not None
    # 15m reference second falls inside the gap: invalid.
    assert h.history.lookup(sym, now_ms=now, interval_sec=900) is None
    # 30m window crosses the gap: invalid.
    assert h.history.lookup(sym, now_ms=now, interval_sec=1800) is None

    # REST kline backfill restores the missing minutes.
    gs, ge = h.history.gaps(sym, min_len_sec=60)[0]
    closes = [(sec, 100.0) for sec in range(gs - 1, ge + 1, 60)]
    h.history.backfill_klines(sym, closes, lo_sec=gs, hi_sec=ge)
    assert h.history.gaps(sym) == []
    filled = h.history.lookup(sym, now_ms=now, interval_sec=900)
    assert filled is not None and filled.quality is HistoryQuality.MIXED
    assert h.history.lookup(sym, now_ms=now, interval_sec=1800) is not None
