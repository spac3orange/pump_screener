from __future__ import annotations

import logging
import signal
from contextlib import asynccontextmanager

import uvicorn
from alembic.config import Config
from fastapi import FastAPI

from alembic import command
from bybit_client import BybitGateway, create_bybit_http
from screener.app_state import AppState
from screener.application.binance_processor import BinanceTickerProcessor
from screener.application.correlator import SignalCorrelator
from screener.application.instrument_registry import InstrumentRegistry
from screener.application.market_feed import MarketFeed
from screener.application.market_processor import MarketDataProcessor
from screener.application.metrics import metrics
from screener.application.notification_service import NotificationService
from screener.application.persistence_service import PersistenceService
from screener.application.price_history import PriceHistoryManager
from screener.application.profiles import ActiveProfile, ProfileService
from screener.application.signal_engine import SignalEngine
from screener.application.trading.service import TradingService
from screener.commercial_runtime import CommercialRuntime, build_commercial
from screener.config import (
    CommercialSettings,
    ScreenerSettings,
    SystemSettings,
    get_commercial_settings,
    get_system_settings,
    get_trading_settings,
)
from screener.domain.exchanges import ALL_EXCHANGES, BINANCE, BYBIT, normalize_exchanges
from screener.infrastructure.binance.mapping import (
    is_crypto_usdt_perpetual,
)
from screener.infrastructure.binance.mapping import (
    map_instrument as map_binance_instrument,
)
from screener.infrastructure.binance.rest import BinanceRestClient
from screener.infrastructure.binance.ws import BinanceTickerStream
from screener.infrastructure.bybit.lifecycle_runtime import TradeLifecycleRuntime
from screener.infrastructure.bybit.rest import BybitRestClient
from screener.infrastructure.bybit.ws import WebSocketManager
from screener.infrastructure.db.profile_repo import ProfileRepository
from screener.infrastructure.db.repositories import Repository, load_effective_settings
from screener.infrastructure.db.session import create_engine, create_session_factory
from screener.infrastructure.db.trading_repo import TradingRepository
from screener.infrastructure.http.app import create_app
from screener.infrastructure.telegram.bot import TelegramBotService
from screener.infrastructure.telegram.formatter import format_signal_message
from screener.logging import log, setup_logging
from screener.workers.market_worker import MarketWorker

logger = logging.getLogger(__name__)


def run_migrations(database_url: str) -> None:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(cfg, "head")


async def build_runtime(
    system: SystemSettings,
) -> tuple[AppState, list[MarketWorker], TelegramBotService | None]:
    import asyncio

    if system.auto_migrate:
        await asyncio.to_thread(run_migrations, system.database_url)
    else:
        logger.info("AUTO_MIGRATE=false: schema migrations are applied manually")

    engine = create_engine(system.database_url)
    session_factory = create_session_factory(engine)
    repo = Repository(session_factory)

    defaults = system.to_screener_settings()
    profiles = ProfileService(ProfileRepository(session_factory), event_log=repo.log_event)
    active_profile = await profiles.bootstrap(lambda: load_effective_settings(repo, defaults))
    screener_settings = active_profile.settings

    rest = BybitRestClient(system.bybit_rest_base)
    history = PriceHistoryManager()
    processor = MarketDataProcessor(history)
    signal_engine = SignalEngine(history, screener_settings)
    correlator = SignalCorrelator(
        window_ms=screener_settings.correlation_window_ms,
        rule_version=lambda: signal_engine.rule_version,
        exchange=BYBIT,
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
        compression=None if system.ws_compression == "none" else "deflate",
        max_queue=system.ws_max_queue,
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

    trading_settings = get_trading_settings()
    trading: TradingService | None = None
    lifecycle: TradeLifecycleRuntime | None = None
    if trading_settings.enabled_module:
        trading_repo = TradingRepository(session_factory)
        gateway: BybitGateway | None = None
        if trading_settings.orders_environment_ok():
            # One private WS (ours) feeds both entry fill hints and the lifecycle monitor.
            gateway = BybitGateway(
                create_bybit_http(
                    api_key=trading_settings.bybit_api_key,
                    api_secret=trading_settings.bybit_api_secret,
                    demo=trading_settings.is_demo,
                ),
                demo=trading_settings.is_demo,
                api_key=trading_settings.bybit_api_key,
                api_secret=trading_settings.bybit_api_secret,
                start_pybit_ws=False,
            )
            lifecycle = TradeLifecycleRuntime(trading_settings, gateway, trading_repo)
        trading = TradingService(trading_settings, trading_repo, gateway=gateway)
        if lifecycle is not None:
            trading.set_position_listener(lifecycle.service.notice_trade_opened)
        logger.info(
            "Trading module configured mode=%s lifecycle=%s",
            trading_settings.bybit_trading_mode,
            "on" if lifecycle is not None else "off",
        )
    else:
        logger.info("Trading module disabled (BYBIT_TRADING_MODE=disabled)")

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
        trading=trading,
        trading_settings=trading_settings,
        profiles=profiles,
        lifecycle=lifecycle.service if lifecycle is not None else None,
        extras={"engine": engine, "lifecycle_runtime": lifecycle},
    )
    profiles.set_apply(state.apply_profile)
    state.apply_profile(active_profile, False)
    correlator.on_signal = state.dispatch_signal
    state.admin_exchanges = await _load_admin_exchanges(repo)

    binance_history = PriceHistoryManager() if system.binance_enabled else None
    histories: dict[str, PriceHistoryManager] = {BYBIT: history}
    if binance_history is not None:
        histories[BINANCE] = binance_history

    commercial_settings = get_commercial_settings()
    if commercial_settings.commercial_enabled:
        state.commercial = await _build_commercial(
            commercial_settings,
            session_factory,
            history,
            active_profile,
            system,
            histories=histories,
        )

    bybit_feed = MarketFeed.from_app_state(state)
    state.feeds[BYBIT] = bybit_feed
    workers = [MarketWorker(state, feed=bybit_feed)]
    if binance_history is not None:
        binance_feed = _build_binance_feed(
            system, state, screener_settings, repo, binance_history
        )
        state.feeds[BINANCE] = binance_feed
        workers.append(MarketWorker(state, feed=binance_feed))
        binance_feed.evaluate_admin = BINANCE in state.admin_exchanges

    tg: TelegramBotService | None = None
    if system.telegram_bot_token and system.admin_id:
        tg = TelegramBotService(system.telegram_bot_token, state)
        notifications.set_send_func(tg.send_signal)
        if trading is not None:
            trading.set_notify(tg.broadcast_text)
        if lifecycle is not None:
            lifecycle.service.set_notify(tg.notify_trade_closed, tg.broadcast_text)
        if state.commercial is not None:
            state.commercial.dispatcher.set_send(tg.send_to_chat)
            state.commercial.payments.set_gateway(tg.stars)
            state.commercial.payments.set_alert(tg.broadcast_text)
            state.commercial.expiry.set_notify(tg.notify_access_ended)
        logger.info("Telegram bot enabled for ADMIN_ID=%s", system.admin_id)
    else:
        logger.warning("Telegram disabled: set TELEGRAM_BOT_TOKEN and ADMIN_ID in .env")

    return state, workers, tg


async def _load_admin_exchanges(repo: Repository) -> set[str]:
    raw = await repo.get_app_setting("admin_exchanges")
    if not raw:
        return set(ALL_EXCHANGES)
    chosen = set(normalize_exchanges(raw.get("exchanges")))
    return chosen or set(ALL_EXCHANGES)


def _build_binance_feed(
    system: SystemSettings,
    state: AppState,
    settings: ScreenerSettings,
    repo: Repository,
    history: PriceHistoryManager,
) -> MarketFeed:
    stats = metrics.feed(BINANCE)
    rest = BinanceRestClient(system.binance_rest_base, stats=stats)
    processor = BinanceTickerProcessor(stats, history)
    signal_engine = SignalEngine(history, settings)
    correlator = SignalCorrelator(
        window_ms=settings.correlation_window_ms,
        rule_version=lambda: signal_engine.rule_version,
        exchange=BINANCE,
        on_signal=state.dispatch_monitor_signal,
    )
    signal_engine.on_matches = correlator.add
    registry = InstrumentRegistry(
        None,
        refresh_sec=system.instrument_refresh_sec,
        exclude_symbols=settings.exclude_symbols,
        instrument_sink=repo,
        fetch=rest.fetch_instruments,
        accept=is_crypto_usdt_perpetual,
        mapper=map_binance_instrument,
        stats=stats,
        exchange=BINANCE,
    )

    async def _noop_msg(_msg: dict, _mono: float) -> None:
        return None

    ws = BinanceTickerStream(
        system.binance_ws_market,
        on_message=_noop_msg,
        stats=stats,
        stale_connection_sec=float(system.stale_connection_sec),
        compression=None if system.ws_compression == "none" else "deflate",
        max_queue=system.ws_max_queue,
    )
    commercial_engine = None
    if state.commercial is not None:
        commercial_engine = state.commercial.engines.get(BINANCE)
    return MarketFeed(
        exchange=BINANCE,
        processor=processor,
        history=history,
        signal_engine=signal_engine,
        correlator=correlator,
        stats=stats,
        commercial_engine=commercial_engine,
        registry=registry,
        ws=ws,
        rest=rest,
        repo=repo,
        notifications=state.notifications,
        primary=False,
        evaluate_admin=BINANCE in state.admin_exchanges,
        kline_concurrency=system.binance_kline_concurrency,
        kline_warmup_limit=system.kline_warmup_limit,
        stale_connection_sec=float(system.stale_connection_sec),
        gap_backfill_min_sec=system.gap_backfill_min_sec,
    )


async def _build_commercial(
    settings: CommercialSettings,
    session_factory,
    history: PriceHistoryManager,
    active_profile: ActiveProfile,
    system: SystemSettings,
    histories: dict[str, PriceHistoryManager] | None = None,
) -> CommercialRuntime:
    def format_signal(signal, signals_24h: int) -> str:
        return format_signal_message(signal, signals_24h=signals_24h)

    rt = build_commercial(
        settings=settings,
        session_factory=session_factory,
        history=history,
        template_hint=active_profile.settings,
        format_signal=format_signal,
        histories=histories,
    )
    profile = active_profile.profile
    await rt.users.bootstrap(
        active_profile.settings,
        source_profile_version_id=profile.current.id if profile is not None else None,
        admin_ids=system.admin_id,
    )
    template = rt.users.template
    for correlator in rt.correlators.values():
        correlator.window_ms = template.correlation_window_ms
    rt.apply_groups(template)
    logger.info(
        "Commercial mode ON: users=%s groups=%s price=%s XTR trial=%sh",
        rt.registry.user_count,
        rt.registry.group_count,
        settings.subscription_price_stars,
        settings.trial_duration_hours,
    )
    return rt


def build_fastapi() -> FastAPI:
    system = get_system_settings()
    setup_logging(level=system.log_level, json_logs=system.log_json)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        state, workers, tg = await build_runtime(system)
        _app.state.app_state = state

        await state.persistence.start()
        await state.notifications.start()
        if state.trading is not None:
            await state.trading.start()
        lifecycle_runtime = state.extras.get("lifecycle_runtime")
        if lifecycle_runtime is not None:
            await lifecycle_runtime.start()
        if state.commercial is not None:
            await state.commercial.start()
        for worker in workers:
            await worker.start()
        if tg is not None:
            await tg.start_polling()
        log(
            logger,
            logging.INFO,
            f"Screener started log_level={system.log_level} "
            f"trading_mode="
            f"{state.trading_settings.bybit_trading_mode if state.trading_settings else 'n/a'}",
            component="main",
            event="started",
        )
        try:
            yield
        finally:
            log(
                logger,
                logging.INFO,
                "Screener shutting down",
                component="main",
                event="shutdown",
            )
            for worker in reversed(workers):
                await worker.stop()
            if state.commercial is not None:
                await state.commercial.stop()
            if lifecycle_runtime is not None:
                await lifecycle_runtime.stop()
            if state.trading is not None:
                await state.trading.stop()
            await state.notifications.stop()
            if tg is not None:
                await tg.stop()
            await state.persistence.stop()
            for feed in state.feeds.values():
                if feed.rest is not None and feed.rest is not state.rest:
                    await feed.rest.aclose()
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
