"""Binance USDⓈ-M Futures public REST (``/fapi``): instruments and klines.

Request weight is shared per IP (2400/min). The used weight from
``X-MBX-USED-WEIGHT-1M`` pauses requests near the limit; 429 (limit hit) and
418 (IP ban) stop all requests for ``Retry-After`` seconds, because hammering
after a 429 escalates to a ban.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from screener.application.metrics import FeedMetrics

logger = logging.getLogger(__name__)

WEIGHT_LIMIT_1M = 2400
WEIGHT_SOFT_LIMIT_1M = 1800
_MAX_WAIT_SEC = 60.0
_DEFAULT_BAN_SEC = {429: 60.0, 418: 300.0}


class BinanceRateLimited(RuntimeError):
    """Requests are paused after a 429/418 from Binance."""


def _interval(value: str) -> str:
    """Bybit-style minute intervals ("1", "5") -> Binance ("1m", "5m")."""
    return f"{value}m" if value.isdigit() else value


class BinanceRestClient:
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
        self.used_weight_1m = 0

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def paused_for_sec(self) -> float:
        return max(0.0, self._paused_until - self._clock())

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        wait = self.paused_for_sec
        if wait > _MAX_WAIT_SEC:
            raise BinanceRateLimited(f"Binance requests paused for {wait:.0f}s")
        if wait > 0:
            await asyncio.sleep(wait)
        resp = await self._client.get(path, params=params)
        self._track_weight(resp)
        if resp.status_code in (429, 418):
            self._pause_after_limit(resp)
            raise BinanceRateLimited(f"Binance HTTP {resp.status_code} on {path}")
        resp.raise_for_status()
        return resp.json()

    def _track_weight(self, resp: httpx.Response) -> None:
        raw = resp.headers.get("x-mbx-used-weight-1m")
        if raw is None:
            return
        try:
            self.used_weight_1m = int(raw)
        except ValueError:
            return
        if self.used_weight_1m >= WEIGHT_SOFT_LIMIT_1M:
            # The window resets on the minute boundary of the exchange clock.
            until_next_minute = 60.0 - (time.time() % 60.0) + 0.5
            self._paused_until = max(self._paused_until, self._clock() + until_next_minute)
            if self._stats is not None:
                self._stats.rest_throttled_total += 1

    def _pause_after_limit(self, resp: httpx.Response) -> None:
        try:
            retry_after = float(resp.headers.get("retry-after", ""))
        except ValueError:
            retry_after = _DEFAULT_BAN_SEC[resp.status_code]
        self._paused_until = max(self._paused_until, self._clock() + retry_after)
        if self._stats is not None:
            self._stats.rest_throttled_total += 1
        logger.warning(
            "Binance REST HTTP %s: pausing requests for %.0fs", resp.status_code, retry_after
        )

    async def fetch_instruments(self) -> list[dict[str, Any]]:
        payload = await self._get("/fapi/v1/exchangeInfo", {})
        symbols = payload.get("symbols") if isinstance(payload, dict) else None
        items = list(symbols or [])
        logger.info("Fetched %s futures instruments from Binance", len(items))
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
        """Klines ascending by open time: ``[openTime, open, high, low, close, ...]``."""
        params: dict[str, Any] = {
            "symbol": symbol,
            "interval": _interval(interval),
            "limit": min(limit, 1500),
        }
        if start_ms is not None:
            params["startTime"] = start_ms
        if end_ms is not None:
            params["endTime"] = end_ms
        rows = await self._get("/fapi/v1/klines", params)
        return list(rows) if isinstance(rows, list) else []
