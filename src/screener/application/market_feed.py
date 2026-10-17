"""One exchange's market path: registry, WS, processor, history, engines."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from screener.application.metrics import metrics
from screener.domain.exchanges import BYBIT


@dataclass
class MarketFeed:
    exchange: str
    processor: Any
    history: Any
    signal_engine: Any
    correlator: Any
    stats: Any
    commercial_engine: Any = None
    registry: Any = None
    ws: Any = None
    rest: Any = None
    repo: Any = None
    notifications: Any = None
    primary: bool = False
    evaluate_admin: bool = True
    kline_concurrency: int = 8
    kline_warmup_limit: int = 62
    stale_connection_sec: float = 30.0
    gap_backfill_min_sec: int = 60

    @classmethod
    def from_app_state(cls, state: Any) -> MarketFeed:
        """Bybit feed from the historical AppState fields (tests and default path)."""
        system = state.system
        return cls(
            exchange=BYBIT,
            processor=state.processor,
            history=state.history,
            signal_engine=state.signal_engine,
            correlator=getattr(state, "correlator", None),
            stats=metrics,
            commercial_engine=getattr(state, "commercial_engine", None),
            registry=getattr(state, "registry", None),
            ws=getattr(state, "ws_manager", None),
            rest=getattr(state, "rest", None),
            repo=getattr(state, "repo", None),
            notifications=getattr(state, "notifications", None),
            primary=True,
            evaluate_admin=True,
            kline_concurrency=int(getattr(system, "kline_warmup_concurrency", 8)),
            kline_warmup_limit=int(getattr(system, "kline_warmup_limit", 62)),
            stale_connection_sec=float(getattr(system, "stale_connection_sec", 30)),
            gap_backfill_min_sec=int(getattr(system, "gap_backfill_min_sec", 60)),
        )
