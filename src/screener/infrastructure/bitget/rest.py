"""Bitget UTA v3 public REST: instruments and candles.

Success is ``code == "00000"`` even when HTTP is 200. HTTP 429 pauses every
request on this client; a pause longer than a minute fails the call instead of
sleeping inside the instrument refresh.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from screener.application.metrics import FeedMetrics

logger = logging.getLogger(__name__)

_MAX_WAIT_SEC = 60.0
_DEFAULT_PAUSE_SEC = 60.0
_KLINE_LIMIT = 1000


class BitgetApiError(RuntimeError):
    """Bitget answered, but ``code`` was not ``00000``."""


class BitgetRateLimited(RuntimeError):
    """Requests are paused after HTTP 429 from Bitget."""


def _interval(value: str) -> str:
    """Bybit-style minute intervals ("1", "5") -> Bitget ("1m", "5m")."""
    return f"{value}m" if value.isdigit() else value


class BitgetRestClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 20.0,
        stats: FeedMetrics | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        clock=time.monotonic,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._base_url, timeout=timeout, transport=transport
        )
        self._stats = stats
        self._clock = clock
        self._paused_until = 0.0

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def paused_for_sec(self) -> float:
        return max(0.0, self._paused_until - self._clock())

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        wait = self.paused_for_sec
        if wait > _MAX_WAIT_SEC:
            raise BitgetRateLimited(f"Bitget requests paused for {wait:.0f}s")
        if wait > 0:
            await asyncio.sleep(wait)
        resp = await self._client.get(path, params=params)
        if resp.status_code == 429:
            self._pause_after_limit(resp)
            raise BitgetRateLimited(f"Bitget HTTP 429 on {path}")
        resp.raise_for_status()
        payload = resp.json()
        if isinstance(payload, dict):
            code = str(payload.get("code", "00000"))
            if code != "00000":
                raise BitgetApiError(f"Bitget {path} code {code}: {payload.get('msg')}")
        return payload

    def _pause_after_limit(self, resp: httpx.Response) -> None:
        try:
            retry_after = float(resp.headers.get("retry-after", ""))
        except ValueError:
            retry_after = _DEFAULT_PAUSE_SEC
        self._paused_until = max(self._paused_until, self._clock() + retry_after)
        if self._stats is not None:
            self._stats.rest_throttled_total += 1
        logger.warning("Bitget REST HTTP 429: pausing requests for %.0fs", retry_after)

    async def fetch_instruments(self) -> list[dict[str, Any]]:
        payload = await self._get("/api/v3/market/instruments", {"category": "USDT-FUTURES"})
        data = payload.get("data") if isinstance(payload, dict) else None
        items = [row for row in (data or []) if isinstance(row, dict)]
        logger.info("Fetched %s futures instruments from Bitget", len(items))
        return items

    async def fetch_kline(
        self,
        symbol: str,
        *,
        interval: str = "1",
        limit: int = 60,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> list[list[Any]]:
        """Candles ascending by open time: ``[openTime, open, high, low, close, ...]``."""
        params: dict[str, Any] = {
            "category": "USDT-FUTURES",
            "symbol": symbol,
            "interval": _interval(interval),
            "limit": str(min(limit, _KLINE_LIMIT)),
        }
        if start_ms is not None:
            params["startTime"] = str(start_ms)
        if end_ms is not None:
            params["endTime"] = str(end_ms)
        payload = await self._get("/api/v3/market/candles", params)
        rows = payload.get("data") if isinstance(payload, dict) else None
        return [list(row) for row in rows] if isinstance(rows, list) else []
