"""Sharded Bitget UTA v3 public ticker stream.

There is no all-market ticker: each symbol is its own ``ticker`` channel.
Shards hold about 40 symbols. One subscribe stays under 4096 bytes and client
messages stay under 10/s. The client sends the text ``ping``; Bitget replies
``pong`` and closes a socket that stays silent for two minutes. A shard
reconnects with backoff and subscribes again. Universe changes subscribe or
unsubscribe on the live socket instead of rebuilding every shard.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field

import orjson
import websockets
from websockets.asyncio.client import ClientConnection

from screener.application.metrics import FeedMetrics

logger = logging.getLogger(__name__)

MessageHandler = Callable[[dict, float], Awaitable[None]]
ConnectHandler = Callable[[list[str]], None]
DisconnectHandler = Callable[[list[str], int | None], None]

_INST_TYPE = "usdt-futures"
_TOPIC = "ticker"
_ARGS_BUDGET = 4096
_MAX_FRAME_BYTES = 1024 * 1024
# Bitget drops the socket after 2 minutes without a ping. Ping sooner than the
# stale window so a quiet shard stays up on ``pong`` instead of reconnecting.
_DEFAULT_PING_SEC = 20.0
_SEND_GAP_SEC = 0.12


def _arg(symbol: str) -> dict[str, str]:
    return {"instType": _INST_TYPE, "topic": _TOPIC, "symbol": symbol}


def _batches(symbols: Sequence[str]) -> list[list[dict[str, str]]]:
    batches: list[list[dict[str, str]]] = []
    current: list[dict[str, str]] = []
    for symbol in symbols:
        trial = [*current, _arg(symbol)]
        if current and len(orjson.dumps(trial)) > _ARGS_BUDGET:
            batches.append(current)
            current = [_arg(symbol)]
        else:
            current = trial
    if current:
        batches.append(current)
    return batches


@dataclass
class _Shard:
    shard_id: int
    desired: set[str]
    ws: ClientConnection | None = None
    task: asyncio.Task[None] | None = None
    connected: bool = False
    last_frame_mono: float | None = None
    last_exchange_ts_ms: int | None = None
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def symbols(self) -> list[str]:
        return sorted(self.desired)


class BitgetTickerStream:
    def __init__(
        self,
        ws_url: str,
        *,
        on_message: MessageHandler,
        stats: FeedMetrics,
        on_connect: ConnectHandler | None = None,
        on_disconnect: DisconnectHandler | None = None,
        symbols_per_connection: int = 40,
        ping_interval_sec: float = _DEFAULT_PING_SEC,
        stale_connection_sec: float = 30.0,
        compression: str | None = None,
        max_queue: int = 1024,
    ) -> None:
        self._ws_url = ws_url
        self.on_message = on_message
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        self._stats = stats
        self._symbols_per_connection = symbols_per_connection
        self._ping_interval_sec = ping_interval_sec
        self._stale_connection_sec = stale_connection_sec
        self._compression = compression
        self._max_queue = max_queue
        self._shards: dict[int, _Shard] = {}
        self._running = False
        self._lock = asyncio.Lock()

    @property
    def connection_count(self) -> int:
        return sum(1 for shard in self._shards.values() if shard.connected)

    @property
    def symbols(self) -> list[str]:
        return sorted(set().union(*(shard.desired for shard in self._shards.values())))

    @property
    def last_message_age_sec(self) -> float | None:
        stamps = [s.last_frame_mono for s in self._shards.values() if s.last_frame_mono]
        if not stamps:
            return None
        return time.monotonic() - max(stamps)

    async def start(self, symbols: Sequence[str]) -> None:
        async with self._lock:
            self._running = True
            await self._apply_universe(set(symbols))
            for shard in self._shards.values():
                if shard.task is None:
                    self._spawn(shard)

    async def update_symbols(self, symbols: Sequence[str]) -> None:
        async with self._lock:
            await self._apply_universe(set(symbols))

    async def stop(self) -> None:
        async with self._lock:
            self._running = False
            shards = list(self._shards.values())
            for shard in shards:
                self._stop_shard(shard)
            await asyncio.gather(
                *[shard.task for shard in shards if shard.task], return_exceptions=True
            )
            self._shards.clear()
            self._stats.ws_connections = 0

    def _stop_shard(self, shard: _Shard) -> None:
        shard.stop_event.set()
        if shard.task is not None:
            shard.task.cancel()

    def _spawn(self, shard: _Shard) -> None:
        shard.task = asyncio.create_task(
            self._run_shard(shard), name=f"bitget-ws-{shard.shard_id}"
        )

    def _sync_connections(self) -> None:
        self._stats.ws_connections = self.connection_count

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
        for offset in range(0, len(added), cap):
            shard = _Shard(shard_id=next_id, desired=set(added[offset : offset + cap]))
            self._shards[next_id] = shard
            if self._running:
                self._spawn(shard)
            next_id += 1
        self._sync_connections()

    async def _send_op(self, shard: _Shard, op: str, symbols: Iterable[str]) -> None:
        ws = shard.ws
        if ws is None or not shard.connected:
            return
        batches = _batches(list(symbols))
        for index, batch in enumerate(batches):
            if index:
                await asyncio.sleep(_SEND_GAP_SEC)
            await ws.send(orjson.dumps({"op": op, "args": batch}).decode())

    async def _run_shard(self, shard: _Shard) -> None:
        backoff = 1.0
        while not shard.stop_event.is_set() and self._running:
            connected_once = False
            try:
                async with websockets.connect(
                    self._ws_url,
                    ping_interval=None,
                    close_timeout=5,
                    max_queue=self._max_queue,
                    max_size=_MAX_FRAME_BYTES,
                    compression=self._compression,
                ) as ws:
                    shard.ws = ws
                    shard.connected = True
                    connected_once = True
                    shard.last_frame_mono = time.monotonic()
                    self._sync_connections()
                    if self.on_connect is not None:
                        self.on_connect(shard.symbols)
                    await self._send_op(shard, "subscribe", shard.symbols)
                    backoff = 1.0
                    ping = asyncio.create_task(self._ping(ws))
                    watch = asyncio.create_task(self._watchdog(shard, ws))
                    try:
                        async for raw in ws:
                            await self._on_raw(shard, raw)
                    finally:
                        ping.cancel()
                        watch.cancel()
                        await asyncio.gather(ping, watch, return_exceptions=True)
            except asyncio.CancelledError:
                self._mark_down(shard, connected_once)
                raise
            except Exception:
                logger.warning("Bitget WS shard %s disconnected", shard.shard_id, exc_info=True)
            self._mark_down(shard, connected_once)
            if shard.stop_event.is_set() or not self._running:
                break
            self._stats.record_reconnect()
            await asyncio.sleep(backoff + random.uniform(0, backoff * 0.2))
            backoff = min(backoff * 2, 60.0)

    def _mark_down(self, shard: _Shard, connected_once: bool) -> None:
        was_connected = shard.connected
        shard.connected = False
        shard.ws = None
        self._sync_connections()
        if connected_once and was_connected and self.on_disconnect is not None:
            self.on_disconnect(shard.symbols, shard.last_exchange_ts_ms)

    async def _ping(self, ws: ClientConnection) -> None:
        while True:
            await asyncio.sleep(self._ping_interval_sec)
            await ws.send("ping")

    async def _watchdog(self, shard: _Shard, ws: ClientConnection) -> None:
        while True:
            await asyncio.sleep(0.2 if self._stale_connection_sec < 1 else 1.0)
            last = shard.last_frame_mono
            if last is not None and time.monotonic() - last > self._stale_connection_sec:
                logger.warning("Bitget WS shard %s stale; reconnecting", shard.shard_id)
                await ws.close()
                return

    async def _on_raw(self, shard: _Shard, raw: str | bytes) -> None:
        mono = time.monotonic()
        shard.last_frame_mono = mono
        self._stats.ws_frames_total += 1
        if raw == "pong" or raw == b"pong":
            return
        try:
            msg = orjson.loads(raw)
        except orjson.JSONDecodeError:
            self._stats.ws_decode_errors_total += 1
            return
        if not isinstance(msg, dict):
            return
        ts = msg.get("ts")
        if isinstance(ts, int):
            shard.last_exchange_ts_ms = ts
        elif isinstance(ts, str) and ts.isdigit():
            shard.last_exchange_ts_ms = int(ts)
        data = msg.get("data")
        if isinstance(data, list) and data:
            await self.on_message(msg, mono)
