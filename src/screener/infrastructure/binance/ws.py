"""Binance all-market ticker stream (``!ticker@arr`` on the ``/market`` endpoint).

One connection carries every USDⓈ-M symbol, so there is nothing to subscribe
or shard. Binance pushes one frame per second with the tickers that changed,
pings every 3 minutes (answered by ``websockets``) and closes connections
after 24 hours; every disconnect reconnects with backoff. Each frame is
awaited by ``on_message`` inside the reader, so nothing is dropped.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable, Sequence

import orjson
import websockets

from screener.application.metrics import FeedMetrics

logger = logging.getLogger(__name__)

MessageHandler = Callable[[dict, float], Awaitable[None]]
ConnectHandler = Callable[[list[str]], None]
DisconnectHandler = Callable[[list[str], int | None], None]

_MAX_FRAME_BYTES = 8 * 1024 * 1024


class BinanceTickerStream:
    def __init__(
        self,
        ws_url: str,
        *,
        on_message: MessageHandler,
        stats: FeedMetrics,
        on_connect: ConnectHandler | None = None,
        on_disconnect: DisconnectHandler | None = None,
        stale_connection_sec: float = 30.0,
        compression: str | None = "deflate",
        max_queue: int = 1024,
    ) -> None:
        self._ws_url = ws_url
        self.on_message = on_message
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        self._stats = stats
        self._stale_connection_sec = stale_connection_sec
        self._compression = compression
        self._max_queue = max_queue
        self._symbols: list[str] = []
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._connected = False
        self._last_frame_mono: float | None = None
        self._last_exchange_ts_ms: int | None = None

    @property
    def connection_count(self) -> int:
        return 1 if self._connected else 0

    @property
    def symbols(self) -> list[str]:
        return list(self._symbols)

    @property
    def last_message_age_sec(self) -> float | None:
        if self._last_frame_mono is None:
            return None
        return time.monotonic() - self._last_frame_mono

    async def start(self, symbols: Sequence[str]) -> None:
        self._symbols = sorted(symbols)
        self._running = True
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="binance-ws")

    async def update_symbols(self, symbols: Sequence[str]) -> None:
        """The stream always carries every symbol; the processor filters the universe."""
        self._symbols = sorted(symbols)

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        self._connected = False
        self._stats.ws_connections = 0

    async def _run(self) -> None:
        backoff = 1.0
        while self._running:
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
                    self._connected = True
                    connected_once = True
                    self._last_frame_mono = time.monotonic()
                    self._stats.ws_connections = 1
                    if self.on_connect is not None:
                        self.on_connect(self.symbols)
                    backoff = 1.0
                    watch = asyncio.create_task(self._watchdog(ws))
                    try:
                        async for raw in ws:
                            mono = time.monotonic()
                            self._last_frame_mono = mono
                            self._stats.ws_frames_total += 1
                            try:
                                msg = orjson.loads(raw)
                            except orjson.JSONDecodeError:
                                self._stats.ws_decode_errors_total += 1
                                continue
                            if not isinstance(msg, dict) or "data" not in msg:
                                continue
                            data = msg["data"]
                            if isinstance(data, list) and data and isinstance(data[0], dict):
                                ts = data[0].get("E")
                                if isinstance(ts, int):
                                    self._last_exchange_ts_ms = ts
                            await self.on_message(msg, mono)
                    finally:
                        watch.cancel()
                        await asyncio.gather(watch, return_exceptions=True)
            except asyncio.CancelledError:
                self._mark_down(connected_once)
                raise
            except Exception:
                logger.warning("Binance WS disconnected", exc_info=True)
            self._mark_down(connected_once)
            if not self._running:
                break
            self._stats.record_reconnect()
            await asyncio.sleep(backoff + random.uniform(0, backoff * 0.2))
            backoff = min(backoff * 2, 60.0)

    def _mark_down(self, connected_once: bool) -> None:
        was_connected = self._connected
        self._connected = False
        self._stats.ws_connections = 0
        if connected_once and was_connected and self.on_disconnect is not None:
            self.on_disconnect(self.symbols, self._last_exchange_ts_ms)

    async def _watchdog(self, ws) -> None:
        while True:
            await asyncio.sleep(1.0)
            last = self._last_frame_mono
            if last is not None and time.monotonic() - last > self._stale_connection_sec:
                logger.warning("Binance WS stale (no frames); reconnecting")
                await ws.close()
                return
