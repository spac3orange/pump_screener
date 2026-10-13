from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from screener.application.metrics import metrics

logger = logging.getLogger(__name__)


def supervise(
    name: str,
    factory: Callable[[], Awaitable[None]],
    *,
    max_backoff_sec: float = 30.0,
) -> asyncio.Task[None]:
    """Run a long-lived coroutine, restarting it with backoff if it crashes or returns."""

    async def runner() -> None:
        backoff = 1.0
        while True:
            try:
                await factory()
                logger.warning("Task %s exited; restarting", name)
            except asyncio.CancelledError:
                raise
            except Exception:
                metrics.record_error()
                logger.exception("Task %s crashed; restarting in %.1fs", name, backoff)
            metrics.task_restarts_total += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff_sec)

    return asyncio.create_task(runner(), name=name)


async def cancel_tasks(tasks: list[asyncio.Task]) -> None:
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    tasks.clear()
