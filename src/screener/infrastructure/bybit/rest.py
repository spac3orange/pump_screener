from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class BybitRestClient:
    def __init__(self, base_url: str, *, timeout: float = 20.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        resp = await self._client.get(path, params=params)
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("retCode") != 0:
            raise RuntimeError(
                f"Bybit API error retCode={payload.get('retCode')} retMsg={payload.get('retMsg')}"
            )
        return payload["result"]

    async def fetch_linear_instruments(self) -> list[dict[str, Any]]:
        """Paginate GET /v5/market/instruments-info for category=linear."""
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"category": "linear", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            result = await self._get("/v5/market/instruments-info", params)
            batch = result.get("list") or []
            items.extend(batch)
            cursor = result.get("nextPageCursor") or None
            if not cursor:
                break
        logger.info("Fetched %s linear instruments from Bybit", len(items))
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
    ) -> list[list[str]]:
        result = await self._get(
            "/v5/market/kline",
            {
                "category": category,
                "symbol": symbol,
                "interval": interval,
                "limit": limit,
            },
        )
        return list(result.get("list") or [])
