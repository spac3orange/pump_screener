from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

import websockets
from websockets.asyncio.client import ClientConnection

from screener.application.metrics import metrics
from screener.infrastructure.bybit.mapping import ticker_topic

logger = logging.getLogger(__name__)

MessageHandler = Callable[[dict], Awaitable[None]]


@dataclass
class ShardState:
    shard_id: int
    symbols: list[str]
    task: asyncio.Task[None] | None = None
    connected: bool = False
    last_message_ts: float | None = None
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)


class WebSocketManager:
    """Manages sharded public linear ticker subscriptions."""

    def __init__(
        self,
        ws_url: str,
        *,
        on_message: MessageHandler,
        symbols_per_connection: int = 100,
        args_char_budget: int = 20_000,
        ping_interval_sec: float = 20.0,
        stale_connection_sec: float = 15.0,
    ) -> None:
        self._ws_url = ws_url
        self._on_message = on_message
        self._symbols_per_connection = symbols_per_connection
        self._args_char_budget = args_char_budget
        self._ping_interval_sec = ping_interval_sec
        self._stale_connection_sec = stale_connection_sec
        self._shards: dict[int, ShardState] = {}
        self._symbols: list[str] = []
        self._running = False
        self._lock = asyncio.Lock()

    @property
    def connection_count(self) -> int:
        return sum(1 for s in self._shards.values() if s.connected)

    @property
    def last_message_age_sec(self) -> float | None:
        stamps = [s.last_message_ts for s in self._shards.values() if s.last_message_ts]
        if not stamps:
            return None
        return time.time() - max(stamps)

    async def start(self, symbols: Sequence[str]) -> None:
        async with self._lock:
            self._running = True
            await self._rebalance(list(symbols))

    async def update_symbols(self, symbols: Sequence[str]) -> None:
        async with self._lock:
            if not self._running:
                self._symbols = list(symbols)
                return
            await self._rebalance(list(symbols))

    async def stop(self) -> None:
        async with self._lock:
            self._running = False
            for shard in list(self._shards.values()):
                shard.stop_event.set()
                if shard.task:
                    shard.task.cancel()
            await asyncio.gather(
                *[s.task for s in self._shards.values() if s.task],
                return_exceptions=True,
            )
            self._shards.clear()
            metrics.ws_connections = 0

    async def _rebalance(self, symbols: list[str]) -> None:
        symbols = sorted(set(symbols))
        self._symbols = symbols
        groups = [
            symbols[i : i + self._symbols_per_connection]
            for i in range(0, len(symbols), self._symbols_per_connection)
        ] or [[]]

        # Stop excess shards
        for shard_id in list(self._shards):
            if shard_id >= len(groups) or not groups[shard_id]:
                shard = self._shards.pop(shard_id)
                shard.stop_event.set()
                if shard.task:
                    shard.task.cancel()

        for shard_id, group in enumerate(groups):
            if not group:
                continue
            existing = self._shards.get(shard_id)
            if existing and existing.symbols == group and existing.task and not existing.task.done():
                continue
            if existing:
                existing.stop_event.set()
                if existing.task:
                    existing.task.cancel()
            state = ShardState(shard_id=shard_id, symbols=group)
            state.task = asyncio.create_task(
                self._run_shard(state), name=f"ws-shard-{shard_id}"
            )
            self._shards[shard_id] = state

        metrics.ws_connections = self.connection_count
        logger.info(
            "WS rebalance: %s symbols across %s shards",
            len(symbols),
            len(self._shards),
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

    async def _run_shard(self, state: ShardState) -> None:
        backoff = 1.0
        while not state.stop_event.is_set() and self._running:
            try:
                async with websockets.connect(
                    self._ws_url,
                    ping_interval=None,
                    close_timeout=5,
                    max_queue=1024,
                ) as ws:
                    state.connected = True
                    metrics.ws_connections = self.connection_count
                    backoff = 1.0
                    await self._subscribe_all(ws, state.symbols)
                    ping_task = asyncio.create_task(self._ping_loop(ws, state))
                    stale_task = asyncio.create_task(self._stale_watch(ws, state))
                    try:
                        async for raw in ws:
                            if state.stop_event.is_set():
                                break
                            state.last_message_ts = time.time()
                            try:
                                msg = json.loads(raw)
                            except json.JSONDecodeError:
                                metrics.record_error()
                                continue
                            await self._on_message(msg)
                    finally:
                        ping_task.cancel()
                        stale_task.cancel()
                        await asyncio.gather(ping_task, stale_task, return_exceptions=True)
                        state.connected = False
                        metrics.ws_connections = self.connection_count
            except asyncio.CancelledError:
                raise
            except Exception:
                metrics.record_error()
                metrics.record_reconnect()
                logger.exception("WS shard %s disconnected; reconnecting", state.shard_id)
                state.connected = False
                metrics.ws_connections = self.connection_count
                if state.stop_event.is_set() or not self._running:
                    break
                jitter = random.uniform(0, backoff * 0.2)
                await asyncio.sleep(backoff + jitter)
                backoff = min(backoff * 2, 60.0)

    async def _subscribe_all(self, ws: ClientConnection, symbols: Sequence[str]) -> None:
        for batch in self._subscribe_batches(symbols):
            payload = {"op": "subscribe", "args": batch}
            await ws.send(json.dumps(payload))
            await asyncio.sleep(0.05)

    async def _ping_loop(self, ws: ClientConnection, state: ShardState) -> None:
        while not state.stop_event.is_set():
            await asyncio.sleep(self._ping_interval_sec)
            try:
                await ws.send(json.dumps({"op": "ping"}))
            except Exception:
                return

    async def _stale_watch(self, ws: ClientConnection, state: ShardState) -> None:
        while not state.stop_event.is_set():
            await asyncio.sleep(1.0)
            if state.last_message_ts is None:
                continue
            if time.time() - state.last_message_ts > self._stale_connection_sec:
                logger.warning("WS shard %s stale; forcing close", state.shard_id)
                await ws.close()
                return
