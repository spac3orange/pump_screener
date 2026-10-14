"""Bybit V5 private WebSocket (asyncio): auth, subscribe, heartbeat, reconnect.

One connection carries ``order``, ``execution`` and ``position``. The API secret is used
only to sign the auth payload and never logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import websockets

logger = logging.getLogger(__name__)

PRIVATE_WS_MAINNET = "wss://stream.bybit.com/v5/private"
PRIVATE_WS_DEMO = "wss://stream-demo.bybit.com/v5/private"
DEFAULT_TOPICS = ("order", "execution", "position")

MessageHandler = Callable[[dict[str, Any]], Awaitable[None]]
StatusHandler = Callable[[bool], None]


class PrivateStreamError(Exception):
    """Auth / subscribe refused or protocol error (message is safe to log)."""


def auth_payload(api_key: str, api_secret: str, *, now: float | None = None) -> dict[str, Any]:
    expires = int(((now if now is not None else time.time()) + 10) * 1000)
    signature = hmac.new(
        api_secret.encode(), f"GET/realtime{expires}".encode(), hashlib.sha256
    ).hexdigest()
    return {"op": "auth", "args": [api_key, expires, signature]}


class BybitPrivateStream:
    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        demo: bool,
        on_message: MessageHandler,
        on_status: StatusHandler | None = None,
        topics: Sequence[str] = DEFAULT_TOPICS,
        ping_interval_sec: float = 20.0,
        stale_after_sec: float = 60.0,
        max_backoff_sec: float = 60.0,
        url: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._url = url or (PRIVATE_WS_DEMO if demo else PRIVATE_WS_MAINNET)
        self._on_message = on_message
        self._on_status = on_status
        self._topics = list(topics)
        self._ping_interval = ping_interval_sec
        self._stale_after = stale_after_sec
        self._max_backoff = max_backoff_sec
        self._connected = False
        self.reconnects = 0
        self.last_message_at: float | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    def _set_status(self, connected: bool) -> None:
        if connected == self._connected:
            return
        self._connected = connected
        if self._on_status is not None:
            try:
                self._on_status(connected)
            except Exception:
                logger.warning("Private WS status callback failed", exc_info=True)

    async def run(self) -> None:
        """Connect forever: exponential backoff with full jitter between attempts."""
        attempt = 0
        while True:
            started = time.monotonic()
            try:
                await self._session()
            except asyncio.CancelledError:
                self._set_status(False)
                raise
            except PrivateStreamError as exc:
                logger.error("Bybit private WS refused: %s", exc)
            except Exception as exc:
                logger.warning("Bybit private WS disconnected: %s", type(exc).__name__)
            self._set_status(False)
            if time.monotonic() - started > 60:
                attempt = 0
            attempt += 1
            self.reconnects += 1
            delay = random.uniform(0.5, 1.0) * min(self._max_backoff, 2 ** min(attempt, 6))
            await asyncio.sleep(delay)

    async def _session(self) -> None:
        async with websockets.connect(
            self._url, ping_interval=None, close_timeout=5, open_timeout=15
        ) as ws:
            await ws.send(json.dumps(auth_payload(self._api_key, self._api_secret)))
            await self._expect(ws, "auth")
            await ws.send(json.dumps({"op": "subscribe", "args": self._topics}))
            await self._expect(ws, "subscribe")
            logger.info("Bybit private WS connected topics=%s", ",".join(self._topics))
            self._set_status(True)
            heartbeat = asyncio.create_task(self._heartbeat(ws), name="bybit-private-ping")
            try:
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=self._stale_after)
                    self.last_message_at = time.monotonic()
                    message = json.loads(raw)
                    if isinstance(message, dict) and message.get("topic"):
                        try:
                            await self._on_message(message)
                        except Exception:
                            logger.warning("Private WS handler failed", exc_info=True)
            finally:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat

    async def _expect(self, ws: Any, op: str) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.monotonic()))
            message = json.loads(raw)
            if not isinstance(message, dict) or message.get("op") != op:
                continue
            if message.get("success"):
                return
            raise PrivateStreamError(f"{op} failed: {message.get('ret_msg') or 'unknown'}")
        raise PrivateStreamError(f"{op} timeout")

    async def _heartbeat(self, ws: Any) -> None:
        while True:
            await asyncio.sleep(self._ping_interval)
            await ws.send(json.dumps({"op": "ping"}))
