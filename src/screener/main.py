from __future__ import annotations

import logging
import signal
from contextlib import asynccontextmanager

import uvicorn
from alembic.config import Config
from fastapi import FastAPI

from alembic import command
from screener.app_state import AppState
from screener.application.correlator import SignalCorrelator
from screener.application.instrument_registry import InstrumentRegistry
from screener.application.market_processor import MarketDataProcessor
from screener.application.notification_service import NotificationService
from screener.application.persistence_service import PersistenceService
from screener.application.price_history import PriceHistoryManager
from screener.application.signal_engine import SignalEngine
from screener.config import SystemSettings, get_system_settings
from screener.infrastructure.bybit.rest import BybitRestClient
from screener.infrastructure.bybit.ws import WebSocketManager
from screener.infrastructure.db.repositories import Repository, load_effective_settings
from screener.infrastructure.db.session import create_engine, create_session_factory
from screener.infrastructure.http.app import create_app
from screener.infrastructure.telegram.bot import TelegramBotService
from screener.logging import setup_logging
from screener.workers.market_worker import MarketWorker

logger = logging.getLogger(__name__)


def run_migrations(database_url: str) -> None:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(cfg, "head")


async def build_runtime(system: SystemSettings) -> tuple[AppState, MarketWorker, TelegramBotService | None]:
    import asyncio

    await asyncio.to_thread(run_migrations, system.database_url)

    engine = create_engine(system.database_url)
    session_factory = create_session_factory(engine)
    repo = Repository(session_factory)

    defaults = system.to_screener_settings()
    screener_settings = await load_effective_settings(repo, defaults)

    rest = BybitRestClient(system.bybit_rest_base)
    history = PriceHistoryManager()
    processor = MarketDataProcessor(history)
    signal_engine = SignalEngine(history, screener_settings)
    correlator = SignalCorrelator(
        window_ms=screener_settings.correlation_window_ms,
        rule_version=lambda: signal_engine.rule_version,
    )
    signal_engine.on_matches = correlator.add

    async def _noop_msg(_msg: dict, _mono: float) -> None:
        return None

    ws_manager = WebSocketManager(
        system.bybit_ws_linear,
        on_message=_noop_msg,
        symbols_per_connection=system.ws_symbols_per_connection,
        args_char_budget=system.ws_args_char_budget,
        ping_interval_sec=system.ws_ping_interval_sec,
        stale_connection_sec=float(system.stale_connection_sec),
        ack_timeout_sec=system.ws_ack_timeout_sec,
    )

    registry = InstrumentRegistry(
        rest,
        refresh_sec=system.instrument_refresh_sec,
        exclude_symbols=screener_settings.exclude_symbols,
        instrument_sink=repo,
    )

    persistence = PersistenceService(repo)
    notifications = NotificationService(
        chat_ids=system.admin_id,
        counter=repo,
        persistence=persistence,
        count_timeout_sec=system.signal_count_db_timeout_sec,
    )

    state = AppState(
        system=system,
        repo=repo,
        rest=rest,
        history=history,
        processor=processor,
        signal_engine=signal_engine,
        correlator=correlator,
        registry=registry,
        ws_manager=ws_manager,
        persistence=persistence,
        notifications=notifications,
        extras={"engine": engine},
    )
    correlator.on_signal = state.dispatch_signal

    tg: TelegramBotService | None = None
    if system.telegram_bot_token and system.admin_id:
        tg = TelegramBotService(system.telegram_bot_token, state)
        notifications.set_send_func(tg.send_signal)
        logger.info("Telegram bot enabled for ADMIN_ID=%s", system.admin_id)
    else:
        logger.warning(
            "Telegram disabled: set TELEGRAM_BOT_TOKEN and ADMIN_ID in .env"
        )

    worker = MarketWorker(state)
    return state, worker, tg


def build_fastapi() -> FastAPI:
    system = get_system_settings()
    setup_logging(level=system.log_level, json_logs=system.log_json)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        state, worker, tg = await build_runtime(system)
        _app.state.app_state = state

        await state.persistence.start()
        await state.notifications.start()
        await worker.start()
        if tg is not None:
            await tg.start_polling()
        logger.info("Screener started")
        try:
            yield
        finally:
            logger.info("Screener shutting down")
            await worker.stop()
            await state.notifications.stop()
            if tg is not None:
                await tg.stop()
            await state.persistence.stop()
            await state.rest.aclose()
            engine = state.extras.get("engine")
            if engine is not None:
                await engine.dispose()

    return create_app(None, lifespan=lifespan)


def run() -> None:
    system = get_system_settings()
    setup_logging(level=system.log_level, json_logs=system.log_json)
    app = build_fastapi()
    config = uvicorn.Config(
        app,
        host=system.app_host,
        port=system.app_port,
        log_level=system.log_level.lower(),
    )
    server = uvicorn.Server(config)

    def _handle_signal(signum, _frame):
        logger.info("Received signal %s; requesting shutdown", signum)
        server.should_exit = True

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    server.run()


if __name__ == "__main__":
    run()
