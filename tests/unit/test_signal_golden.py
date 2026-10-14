"""Functional regression: a fixed market path must keep producing the same signals.

Regenerate the golden file only when a logic change is intentional:
    python -m tests.unit.test_signal_golden --write
"""

from __future__ import annotations

import asyncio
import json
import random
import sys
from pathlib import Path

import pytest

from tests.helpers import T0, Harness

GOLDEN = Path(__file__).resolve().parent.parent / "fixtures" / "signal_golden.json"
SYMBOLS = [f"G{i:02d}USDT" for i in range(30)]
SECONDS = 2400


def _scripted(sym_idx: int, t: int, price: float, base: float) -> float:
    if sym_idx == 3 and t == 800:
        return base * 1.45  # PUMP 12m
    if sym_idx == 5 and 1 <= t <= 1800:
        return base * (1 - 0.12 * t / 1800)  # slow DROP 30m
    if sym_idx == 7:
        if t == 1200:
            return base * 0.55  # DUMP 15m
        if t == 1205:
            return base
    if sym_idx == 9:
        if t == 900:
            return base * 1.45
        if t == 1300:
            return base * 1.55  # escalation after cooldown
    return price


async def _run() -> list[dict]:
    h = Harness()
    rnd = random.Random(42)
    base = [100.0 * (1 + i / 10) for i in range(len(SYMBOLS))]
    prices = list(base)
    for i, sym in enumerate(SYMBOLS):
        await h.send(sym, "snapshot", T0, lastPrice=f"{prices[i]:.6f}", turnover24h="5000000")
    for t in range(1, SECONDS + 1):
        ts = T0 + t * 1000
        for i, sym in enumerate(SYMBOLS):
            new = _scripted(i, t, prices[i], base[i])
            if new == prices[i] and rnd.random() < 0.2 and i not in (5, 9):
                new = prices[i] * (1 + (rnd.random() - 0.5) * 0.006)
            if new != prices[i]:
                prices[i] = new
                await h.send(sym, "delta", ts, lastPrice=f"{new:.6f}")
        if t == 1200:
            # Short-lived crossing inside one second must not be coalesced away.
            await h.send("G11USDT", "delta", ts + 100, lastPrice=f"{base[11] * 1.5:.6f}")
            await h.send("G11USDT", "delta", ts + 200, lastPrice=f"{base[11] * 1.2:.6f}")
            prices[11] = float(f"{base[11] * 1.2:.6f}")
        await h.drain()
    return [
        {
            "symbol": s.symbol,
            "type": s.signal_type.value,
            "interval_sec": s.interval_sec,
            "change_pct": round(s.change_pct, 6),
            "price": round(s.price, 6),
            "level": s.level,
            "matches": sorted(f"{m.signal_type.value}:{m.interval_sec}" for m in s.matches),
        }
        for s in h.signals
    ]


@pytest.mark.asyncio
async def test_signals_match_golden_fixture() -> None:
    assert GOLDEN.exists(), "run: python -m tests.unit.test_signal_golden --write"
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    actual = await _run()
    assert actual == expected
    assert any(s["symbol"] == "G11USDT" and s["type"] == "PUMP" for s in actual)


if __name__ == "__main__":
    if "--write" in sys.argv:
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        data = asyncio.run(_run())
        GOLDEN.write_text(json.dumps(data, indent=1), encoding="utf-8")
        print(f"wrote {len(data)} signals to {GOLDEN}")
