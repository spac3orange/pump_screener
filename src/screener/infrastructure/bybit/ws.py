from __future__ import annotations

import asyncio
import itertools
import logging
import random
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field

import orjson
import websockets
from websockets.asyncio.client import ClientConnection

from screener.application.metrics import metrics
from screener.infrastructure.bybit.mapping import ticker_topic

logger = logging.getLogger(__name__)

MessageHandler = Callable[[dict, float], Awaitable[None]]
ConnectHandler = Callable[[list[str]], None]
DisconnectHandler = Callable[[list[str], int | None], None]

_TOPIC_PREFIX = "tickers."


@dataclass
class _Pending:
    op: str
    topics: list[str]
    sent_mono: float
    retried: bool = False


@dataclass
class ShardState:
    shard_id: int
    desired: set[str]
    subscribed: set[str] = field(default_factory=set)
    pending: dict[str, _Pending] = field(default_factory=dict)
    ws: ClientConnection | None = None
    task: asyncio.Task[None] | None = None
    connected: bool = False
    last_frame_mono: float | None = None
    last_exchange_ts_ms: int | None = None
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def symbols(self) -> list[str]:
        return sorted(self.desired)


class WebSocketManager:
    """Sharded public linear ticker subscriptions with live subscribe/unsubscribe.

    Every frame is handed to ``on_message`` inside the shard reader and awaited,
    so nothing is dropped; a slow consumer applies TCP backpressure instead.
    Connection freshness uses ``time.monotonic()`` over any frame (data or pong).
    """

    def __init__(
        self,
        ws_url: str,
        *,
        on_message: MessageHandler,
        on_connect: ConnectHandler | None = None,
        on_disconnect: DisconnectHandler | None = None,
        symbols_per_connection: int = 100,
        args_char_budget: int = 20_000,
        ping_interval_sec: float = 10.0,
        stale_connection_sec: float = 30.0,
        ack_timeout_sec: float = 10.0,
    ) -> None:
        self._ws_url = ws_url
        self.on_message = on_message
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        self._symbols_per_connection = symbols_per_connection
        self._args_char_budget = args_char_budget
        self._ping_interval_sec = ping_interval_sec
        self._stale_connection_sec = stale_connection_sec
        self._ack_timeout_sec = ack_timeout_sec
        self._shards: dict[int, ShardState] = {}
        self._running = False
        self._lock = asyncio.Lock()
        self._req_ids = itertools.count(1)

    @property
    def connection_count(self) -> int:
        return sum(1 for s in self._shards.values() if s.connected)

    @property
    def symbols(self) -> list[str]:
        return sorted(set().union(*(s.desired for s in self._shards.values())))

    @property
    def last_message_age_sec(self) -> float | None:
        stamps = [s.last_frame_mono for s in self._shards.values() if s.last_frame_mono]
        if not stamps:
            return None
        return time.monotonic() - max(stamps)

    def shard_summary(self) -> list[dict]:
        return [
            {
                "shard": s.shard_id,
                "connected": s.connected,
                "desired": len(s.desired),
                "subscribed": len(s.subscribed),
                "pending_acks": len(s.pending),
            }
            for s in self._shards.values()
        ]

    async def start(self, symbols: Sequence[str]) -> None:
        async with self._lock:
            self._running = True
            await self._apply_universe(set(symbols))
            # Shards planned by update_symbols() before start() have no task yet.
            for shard in self._shards.values():
                if shard.task is None:
                    self._spawn_shard_task(shard)

    async def update_symbols(self, symbols: Sequence[str]) -> None:
        async with self._lock:
            await self._apply_universe(set(symbols))

    async def stop(self) -> None:
        async with self._lock:
            self._running = False
            shards = list(self._shards.values())
            for shard in shards:
                self._stop_shard(shard)
            await asyncio.gather(*[s.task for s in shards if s.task], return_exceptions=True)
            self._shards.clear()
            metrics.ws_connections = 0

    def _stop_shard(self, shard: ShardState) -> None:
        shard.stop_event.set()
        if shard.task:
            shard.task.cancel()

    def _start_shard(self, shard_id: int, symbols: set[str]) -> ShardState:
        state = ShardState(shard_id=shard_id, desired=set(symbols))
        self._shards[shard_id] = state
        if self._running:
            self._spawn_shard_task(state)
        return state

    def _spawn_shard_task(self, state: ShardState) -> None:
        state.task = asyncio.create_task(
            self._run_shard(state), name=f"ws-shard-{state.shard_id}"
        )
        state.task.add_done_callback(self._on_shard_done)

    @staticmethod
    def _on_shard_done(task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception() is not None:
            metrics.record_error()
            logger.error("WS shard task %s died", task.get_name(), exc_info=task.exception())

    async def _apply_universe(self, symbols: set[str]) -> None:
        current: set[str] = set().union(*(s.desired for s in self._shards.values()))
        removed = current - symbols
        added = sorted(symbols - current)

        for shard in list(self._shards.values()):
            gone = shard.desired & removed
            if not gone:
                continue
            shard.desired -= gone
            if not shard.desired:
                self._stop_shard(shard)
                self._shards.pop(shard.shard_id, None)
                continue
            await self._send_op(shard, "unsubscribe", sorted(gone))

        cap = self._symbols_per_connection
        for shard in sorted(self._shards.values(), key=lambda s: s.shard_id):
            if not added:
                break
            room = cap - len(shard.desired)
            if room <= 0:
                continue
            chunk, added = added[:room], added[room:]
            shard.desired.update(chunk)
            await self._send_op(shard, "subscribe", chunk)

        next_id = max(self._shards, default=-1) + 1
        for i in range(0, len(added), cap):
            self._start_shard(next_id, set(added[i : i + cap]))
            next_id += 1

        metrics.ws_connections = self.connection_count
        logger.info(
            "WS universe: %s symbols across %s shards (removed=%s)",
            len(symbols),
            len(self._shards),
            len(removed),
        )

    def _subscribe_batches(self, symbols: Sequence[str]) -> list[list[str]]:
        batches: list[list[str]] = []
        current: list[str] = []
        current_len = 2  # []
        for symbol in symbols:
            topic = ticker_topic(symbol)
            # Approximate JSON array contribution: "topic",
            addition = len(topic) + 3
            if current and current_len + addition > self._args_char_budget:
                batches.append(current)
                current = []
                current_len = 2
            current.append(topic)
            current_len += addition
        if current:
            batches.append(current)
        return batches

    async def _send_op(
        self,
        shard: ShardState,
        op: str,
        symbols: Iterable[str],
        *,
        retried: bool = False,
    ) -> None:
        ws = shard.ws
        if ws is None or not shard.connected:
            return  # applied from ``desired`` on the next connect
        for batch in self._subscribe_batches(list(symbols)):
            req_id = f"{shard.shard_id}-{next(self._req_ids)}"
            shard.pending[req_id] = _Pending(op, batch, time.monotonic(), retried)
            await ws.send(orjson.dumps({"req_id": req_id, "op": op, "args": batch}).decode())
            await asyncio.sleep(0.05)

    async def _handle_control(self, shard: ShardState, msg: dict) -> None:
        req_id = msg.get("req_id")
        op = msg.get("op")
        if op not in {"subscribe", "unsubscribe"} or req_id is None:
            return  # pong or other control frame; freshness already recorded
        pending = shard.pending.pop(str(req_id), None)
        if pending is None:
            return
        symbols = [t[len(_TOPIC_PREFIX) :] for t in pending.topics]
        if msg.get("success"):
            metrics.subscribe_acks_total += 1
            if pending.op == "subscribe":
                shard.subscribed.update(symbols)
            else:
                shard.subscribed.difference_update(symbols)
            return
        metrics.subscribe_failures_total += 1
        logger.warning(
            "WS shard %s %s failed for %s topics: %s",
            shard.shard_id,
            pending.op,
            len(symbols),
            msg.get("ret_msg"),
        )
        if pending.op != "subscribe":
            return
        if len(symbols) > 1 or not pending.retried:
            # Isolate a bad topic: retry one by one.
            for symbol in symbols:
                if symbol in shard.desired:
                    await self._send_op(shard, "subscribe", [symbol], retried=True)
        else:
            shard.desired.discard(symbols[0])
            logger.error("Dropping unsubscribable symbol %s", symbols[0])

    async def _run_shard(self, state: ShardState) -> None:
        backoff = 1.0
        while not state.stop_event.is_set() and self._running:
            connected_once = False
            try:
                async with websockets.connect(
                    self._ws_url,
                    ping_interval=None,
                    close_timeout=5,
                    max_queue=1024,
                ) as ws:
                    state.ws = ws
                    state.connected = True
                    connected_once = True
                    state.subscribed.clear()
                    state.pending.clear()
                    state.last_frame_mono = time.monotonic()
                    metrics.ws_connections = self.connection_count
                    if self.on_connect is not None:
                        self.on_connect(state.symbols)
                    await self._send_op(state, "subscribe", state.symbols)
                    backoff = 1.0
                    ping_task = asyncio.create_task(self._ping_loop(ws, state))
                    watch_task = asyncio.create_task(self._watchdog(ws, state))
                    try:
                        async for raw in ws:
                            if state.stop_event.is_set():
                                break
                            mono = time.monotonic()
                            state.last_frame_mono = mono
                            metrics.ws_frames_total += 1
                            try:
                                msg = orjson.loads(raw)
                            except orjson.JSONDecodeError:
                                metrics.ws_decode_errors_total += 1
                                continue
                            if "topic" in msg:
                                ts = msg.get("ts")
                                if ts is not None:
                                    state.last_exchange_ts_ms = int(ts)
                                await self.on_message(msg, mono)
                            else:
                                await self._handle_control(state, msg)
                    finally:
                        ping_task.cancel()
                        watch_task.cancel()
                        await asyncio.gather(ping_task, watch_task, return_exceptions=True)
            except asyncio.CancelledError:
                self._mark_down(state, connected_once)
                raise
            except Exception:
                metrics.record_error()
                logger.warning("WS shard %s disconnected", state.shard_id, exc_info=True)
            self._mark_down(state, connected_once)
            if state.stop_event.is_set() or not self._running:
                break
            metrics.record_reconnect()
            jitter = random.uniform(0, backoff * 0.2)
            await asyncio.sleep(backoff + jitter)
            backoff = min(backoff * 2, 60.0)

    def _mark_down(self, state: ShardState, connected_once: bool) -> None:
        was_connected = state.connected
        state.connected = False
        state.ws = None
        state.subscribed.clear()
        state.pending.clear()
        metrics.ws_connections = self.connection_count
        if connected_once and was_connected and self.on_disconnect is not None:
            self.on_disconnect(state.symbols, state.last_exchange_ts_ms)

    async def _ping_loop(self, ws: ClientConnection, state: ShardState) -> None:
        while not state.stop_event.is_set():
            await asyncio.sleep(self._ping_interval_sec)
            try:
                await ws.send('{"op":"ping"}')
            except Exception:
                return

    async def _watchdog(self, ws: ClientConnection, state: ShardState) -> None:
        while not state.stop_event.is_set():
            await asyncio.sleep(1.0)
            now = time.monotonic()
            last = state.last_frame_mono
            if last is not None and now - last > self._stale_connection_sec:
                logger.warning("WS shard %s stale (no frames); reconnecting", state.shard_id)
                await ws.close()
                return
            expired = [
                rid for rid, p in state.pending.items() if now - p.sent_mono > self._ack_timeout_sec
            ]
            if expired:
                logger.warning(
                    "WS shard %s: %s subscription acks timed out; reconnecting",
                    state.shard_id,
                    len(expired),
                )
                await ws.close()
                return
