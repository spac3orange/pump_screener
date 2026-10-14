from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from screener.logging import log

logger = logging.getLogger(__name__)


class BybitRestClient:
    def __init__(self, base_url: str, *, timeout: float = 20.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            resp = await self._client.get(path, params=params)
            resp.raise_for_status()
            payload = resp.json()
        except httpx.TimeoutException as exc:
            duration_ms = round((time.perf_counter() - started) * 1000, 1)
            log(
                logger,
                logging.WARNING,
                f"Bybit REST timeout path={path} duration_ms={duration_ms} "
                f"limit={self._timeout}s err={type(exc).__name__}",
                component="bybit_rest",
                event="http_timeout",
                duration_ms=duration_ms,
            )
            raise
        except httpx.HTTPError:
            duration_ms = round((time.perf_counter() - started) * 1000, 1)
            log(
                logger,
                logging.ERROR,
                f"Bybit REST HTTP error path={path} duration_ms={duration_ms}",
                component="bybit_rest",
                event="http_error",
                duration_ms=duration_ms,
                exc_info=True,
            )
            raise

        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        if payload.get("retCode") != 0:
            log(
                logger,
                logging.ERROR,
                f"Bybit API retCode={payload.get('retCode')} path={path}",
                component="bybit_rest",
                event="api_error",
                duration_ms=duration_ms,
                reason=str(payload.get("retMsg") or ""),
            )
            raise RuntimeError(
                f"Bybit API error retCode={payload.get('retCode')} "
                f"retMsg={payload.get('retMsg')}"
            )
        log(
            logger,
            logging.DEBUG,
            f"Bybit REST ok path={path} duration_ms={duration_ms}",
            component="bybit_rest",
            event="http_ok",
            duration_ms=duration_ms,
        )
        return payload["result"]

    async def fetch_linear_instruments(self) -> list[dict[str, Any]]:
        """Paginate GET /v5/market/instruments-info for category=linear."""
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        page = 0
        log(
            logger,
            logging.INFO,
            "Fetching linear instruments from Bybit",
            component="bybit_rest",
            event="instruments_fetch_start",
        )
        while True:
            params: dict[str, Any] = {"category": "linear", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            result = await self._get("/v5/market/instruments-info", params)
            batch = result.get("list") or []
            items.extend(batch)
            page += 1
            cursor = result.get("nextPageCursor") or None
            log(
                logger,
                logging.DEBUG,
                f"Instruments page={page} batch={len(batch)} total={len(items)}",
                component="bybit_rest",
                event="instruments_page",
                count=len(items),
            )
            if not cursor:
                break
        log(
            logger,
            logging.INFO,
            f"Fetched {len(items)} linear instruments from Bybit ({page} pages)",
            component="bybit_rest",
            event="instruments_fetch_done",
            count=len(items),
        )
        return items

    async def fetch_tickers(self, *, category: str = "linear") -> list[dict[str, Any]]:
        result = await self._get("/v5/market/tickers", {"category": category})
        return list(result.get("list") or [])

    async def fetch_kline(
        self,
        symbol: str,
        *,
        interval: str = "1",
        limit: int = 60,
        category: str = "linear",
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> list[list[str]]:
        params: dict[str, Any] = {
            "category": category,
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        }
        if start_ms is not None:
            params["start"] = start_ms
        if end_ms is not None:
            params["end"] = end_ms
        result = await self._get("/v5/market/kline", params)
        return list(result.get("list") or [])
