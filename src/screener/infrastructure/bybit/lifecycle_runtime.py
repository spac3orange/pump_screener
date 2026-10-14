"""Composition of the position lifecycle: private WS → router → PositionLifecycleService."""

from __future__ import annotations

import asyncio

from bybit_client import BybitGateway
from bybit_client.private_ws import BybitPrivateStream
from screener.application.metrics import metrics
from screener.application.supervisor import cancel_tasks, supervise
from screener.application.trading.lifecycle import (
    LifecycleConfig,
    LifecycleStore,
    PositionLifecycleService,
)
from screener.config import TradingSettings
from screener.infrastructure.bybit.lifecycle_adapter import (
    BybitLifecycleGateway,
    PrivateMessageRouter,
)


class TradeLifecycleRuntime:
    """Owns the private stream task; independent of Trading ON/OFF."""

    def __init__(
        self, settings: TradingSettings, gateway: BybitGateway, store: LifecycleStore
    ) -> None:
        self.service = PositionLifecycleService(
            BybitLifecycleGateway(gateway),
            store,
            LifecycleConfig(
                mode=settings.bybit_trading_mode,
                notify_closes=settings.trading_notify_closes,
                correlation_window_sec=settings.lifecycle_correlation_window_sec,
                pnl_wait_sec=settings.lifecycle_pnl_wait_sec,
                reconcile_interval_sec=settings.lifecycle_reconcile_interval_sec,
            ),
        )
        router = PrivateMessageRouter(gateway, self.service)
        self.stream = BybitPrivateStream(
            api_key=settings.bybit_api_key,
            api_secret=settings.bybit_api_secret,
            demo=settings.is_demo,
            on_message=router.handle,
            on_status=self._on_status,
        )
        self._tasks: list[asyncio.Task] = []
        self._was_connected = False

    def _on_status(self, connected: bool) -> None:
        if not connected and self._was_connected:
            metrics.private_ws_reconnects_total += 1
        self._was_connected = connected
        self.service.on_stream_status(connected)

    async def start(self) -> None:
        # Startup recovery may take several REST calls per trade: keep it off the lifespan.
        self._tasks.append(asyncio.create_task(self._boot(), name="trade-lifecycle-boot"))

    async def _boot(self) -> None:
        # Reconcile from REST first; the stream's first connect reconciles again,
        # covering any close that happened between the two.
        await self.service.start()
        self._tasks.append(supervise("bybit-private-ws", self.stream.run))

    async def stop(self) -> None:
        await cancel_tasks(self._tasks)
        await self.service.stop()
