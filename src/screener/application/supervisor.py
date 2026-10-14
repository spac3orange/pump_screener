from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from screener.application.metrics import metrics
from screener.logging import log

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
        log(
            logger,
            logging.INFO,
            f"Supervised task started: {name}",
            component="supervisor",
            event="task_start",
            reason=name,
        )
        while True:
            try:
                await factory()
                log(
                    logger,
                    logging.WARNING,
                    f"Task {name} exited; restarting",
                    component="supervisor",
                    event="task_exit",
                    reason=name,
                )
            except asyncio.CancelledError:
                log(
                    logger,
                    logging.INFO,
                    f"Supervised task cancelled: {name}",
                    component="supervisor",
                    event="task_cancel",
                    reason=name,
                )
                raise
            except Exception:
                metrics.record_error()
                log(
                    logger,
                    logging.ERROR,
                    f"Task {name} crashed; restarting in {backoff:.1f}s",
                    component="supervisor",
                    event="task_crash",
                    reason=name,
                    exc_info=True,
                )
            metrics.task_restarts_total += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff_sec)

    return asyncio.create_task(runner(), name=name)

async def cancel_tasks(tasks: list[asyncio.Task]) -> None:
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    tasks.clear()
