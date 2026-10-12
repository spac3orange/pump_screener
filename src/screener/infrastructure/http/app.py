from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Any

import orjson
from fastapi import FastAPI, Response

from screener.application.metrics import metrics

if TYPE_CHECKING:
    from screener.app_state import AppState

Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]] | Callable[
    [FastAPI], AsyncIterator[Any]
]


def create_app(
    app_state: AppState | None = None,
    *,
    lifespan: Lifespan | None = None,
) -> FastAPI:
    app = FastAPI(title="Bybit Market Screener", version="0.1.0", lifespan=lifespan)
    app.state.app_state = app_state

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> Response:
        snap = metrics.snapshot()
        ready = bool(snap["ready"])
        body = {"ready": ready, **snap}
        return Response(
            content=orjson.dumps(body),
            media_type="application/json",
            status_code=200 if ready else 503,
        )

    @app.get("/metrics")
    async def metrics_endpoint() -> dict:
        return metrics.snapshot()

    return app
