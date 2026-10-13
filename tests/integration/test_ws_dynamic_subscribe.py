import asyncio

import orjson
import pytest

from screener.infrastructure.bybit.ws import ShardState, WebSocketManager


class _FakeWs:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(orjson.loads(raw))

    async def close(self) -> None:
        pass


async def _noop(_msg: dict, _mono: float) -> None:
    return None


def _live_manager(
    symbols: list[str], per_conn: int = 3
) -> tuple[WebSocketManager, ShardState, _FakeWs]:
    mgr = WebSocketManager("wss://example", on_message=_noop, symbols_per_connection=per_conn)
    shard = ShardState(shard_id=0, desired=set(symbols), subscribed=set(symbols))
    ws = _FakeWs()
    shard.ws = ws  # type: ignore[assignment]
    shard.connected = True
    mgr._shards[0] = shard
    return mgr, shard, ws


@pytest.mark.asyncio
async def test_universe_change_uses_live_subscribe_and_unsubscribe() -> None:
    mgr, shard, ws = _live_manager(["AUSDT", "BUSDT"])
    await mgr.update_symbols(["BUSDT", "CUSDT"])
    ops = [(m["op"], m["args"]) for m in ws.sent]
    assert ("unsubscribe", ["tickers.AUSDT"]) in ops
    assert ("subscribe", ["tickers.CUSDT"]) in ops
    assert shard.desired == {"BUSDT", "CUSDT"}
    assert len(mgr._shards) == 1  # no shard restarted or added

    for msg in ws.sent:
        ack = {"op": msg["op"], "req_id": msg["req_id"], "success": True}
        await mgr._handle_control(shard, ack)
    assert shard.subscribed == {"BUSDT", "CUSDT"}
    assert shard.pending == {}


@pytest.mark.asyncio
async def test_overflow_symbols_go_to_new_shard() -> None:
    mgr, shard, ws = _live_manager(["AUSDT", "BUSDT", "CUSDT"], per_conn=3)
    await mgr.update_symbols(["AUSDT", "BUSDT", "CUSDT", "DUSDT"])
    assert ws.sent == []  # full shard untouched
    assert sorted(mgr._shards) == [0, 1]
    assert mgr._shards[1].desired == {"DUSDT"}


@pytest.mark.asyncio
async def test_start_launches_shards_planned_before_start(monkeypatch) -> None:
    # Registry.start() fires on_change -> update_symbols() before ws.start().
    mgr = WebSocketManager("wss://example", on_message=_noop, symbols_per_connection=2)
    started: list[int] = []

    async def fake_run(state: ShardState) -> None:
        started.append(state.shard_id)

    monkeypatch.setattr(mgr, "_run_shard", fake_run)
    await mgr.update_symbols(["AUSDT", "BUSDT", "CUSDT"])
    assert all(s.task is None for s in mgr._shards.values())

    await mgr.start(["AUSDT", "BUSDT", "CUSDT"])
    await asyncio.sleep(0)
    assert sorted(started) == [0, 1]
    assert all(s.task is not None for s in mgr._shards.values())


@pytest.mark.asyncio
async def test_failed_batch_subscribe_retries_per_topic() -> None:
    mgr, shard, ws = _live_manager([], per_conn=10)
    await mgr.update_symbols(["AUSDT", "BADUSDT"])
    first = ws.sent[-1]
    await mgr._handle_control(
        shard, {"op": "subscribe", "req_id": first["req_id"], "success": False, "ret_msg": "bad"}
    )
    retried = [m["args"] for m in ws.sent[1:]]
    assert retried == [["tickers.AUSDT"], ["tickers.BADUSDT"]]
