from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from screener.application.commercial.group_correlator import GroupCorrelator
from screener.application.commercial.multi_engine import MultiConfigEngine
from screener.application.correlator import SignalCorrelator
from screener.application.market_processor import MarketDataProcessor
from screener.application.price_history import PriceHistoryManager
from screener.application.signal_engine import SignalEngine
from screener.config import ScreenerSettings
from screener.domain.commercial import GroupSignal
from screener.domain.enums import Direction, HistoryQuality, SignalType
from screener.domain.models import MarketSignal, RuleMatch
from screener.workers.market_worker import MarketWorker

T0 = 1_700_000_000_000


def settings(**kwargs) -> ScreenerSettings:
    base = dict(
        enable_long=True,
        long_threshold_pct=40.0,
        long_interval_sec=720,
        enable_short=True,
        short_threshold_pct=10.0,
        short_interval_sec=1800,
        enable_dump=True,
        dump_threshold_pct=40.0,
        dump_interval_sec=900,
        min_turnover_24h_usdt=0,
        cooldown_sec=300,
        correlation_window_ms=0,
    )
    base.update(kwargs)
    return ScreenerSettings(**base)


def ticker(symbol: str, msg_type: str, ts_ms: int, cs: int, **data) -> dict:
    return {
        "topic": f"tickers.{symbol}",
        "type": msg_type,
        "ts": ts_ms,
        "cs": cs,
        "data": {"symbol": symbol, **data},
    }


class Harness:
    """Real processor -> ordered price queue -> history -> engine -> correlator path.

    ``commercial`` adds the multi-user engine on the same history, with one
    group per ``key -> settings`` entry.
    """

    def __init__(
        self, *, commercial: dict[str, ScreenerSettings] | None = None, **settings_kwargs
    ) -> None:
        self.history = PriceHistoryManager()
        self.processor = MarketDataProcessor(self.history)
        cfg = settings(**settings_kwargs)
        self.engine = SignalEngine(self.history, cfg)
        self.signals: list[MarketSignal] = []
        self.correlator = SignalCorrelator(
            window_ms=cfg.correlation_window_ms,
            rule_version=lambda: self.engine.rule_version,
            on_signal=self.signals.append,
        )
        self.engine.on_matches = self.correlator.add
        self.group_signals: list[GroupSignal] = []
        self.commercial: MultiConfigEngine | None = None
        if commercial is not None:
            self.group_correlator = GroupCorrelator(
                window_ms=cfg.correlation_window_ms, on_signal=self.group_signals.append
            )
            self.commercial = MultiConfigEngine(
                self.history, cfg, on_matches=self.group_correlator.add
            )
            self.commercial.set_groups(commercial)
        state = SimpleNamespace(
            system=SimpleNamespace(market_queue_maxsize=100_000),
            processor=self.processor,
            history=self.history,
            signal_engine=self.engine,
            correlator=self.correlator,
            commercial_engine=self.commercial,
        )
        self.worker = MarketWorker(state)  # type: ignore[arg-type]
        self._cs = 0

    async def send(self, symbol: str, msg_type: str, ts_ms: int, **data) -> None:
        self._cs += 1
        await self.worker.on_ws_message(ticker(symbol, msg_type, ts_ms, self._cs, **data), 0.0)

    async def drain(self) -> None:
        queue = self.worker.queue
        while not queue.empty():
            self.worker.process_event(queue.get_nowait())
            queue.task_done()


def make_signal(
    signal_type: SignalType,
    interval_sec: int,
    change_pct: float,
    *,
    symbol: str = "RSRUSDT",
    prev_price: float = 1.0,
    price: float = 1.0,
    extra: tuple[RuleMatch, ...] = (),
    exchange: str = "bybit",
) -> MarketSignal:
    primary = RuleMatch(
        signal_type=signal_type,
        interval_sec=interval_sec,
        threshold_pct=10.0,
        change_pct=change_pct,
        prev_price=prev_price,
        price=price,
        detected_ts_ms=T0,
        history_quality=HistoryQuality.LIVE,
    )
    direction = Direction.UP if signal_type is SignalType.PUMP else Direction.DOWN
    return MarketSignal(
        symbol=symbol,
        direction=direction,
        primary=primary,
        matches=(primary, *extra),
        detected_at=datetime(2026, 10, 12, 8, 4, tzinfo=UTC),
        rule_version="abc",
        exchange=exchange,
    )
