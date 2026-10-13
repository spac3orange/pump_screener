from __future__ import annotations

import time
from array import array
from dataclasses import dataclass, field

_LATENCY_SAMPLES = 2048

_RATE_FIELDS = (
    "ws_frames_total",
    "ticker_messages_total",
    "live_price_events_total",
    "engine_evaluations_total",
    "history_lookups_total",
)


@dataclass
class Metrics:
    """Process-local counters. Single event loop, so no locking."""

    instruments_active: int = 0
    ws_connections: int = 0
    ws_reconnects: int = 0
    ws_frames_total: int = 0
    ws_decode_errors_total: int = 0
    ticker_messages_total: int = 0
    snapshot_seeds_total: int = 0
    live_price_events_total: int = 0
    enrichment_updates_total: int = 0
    deltas_before_snapshot_total: int = 0
    out_of_order_total: int = 0
    # Raw protocol messages are never dropped; kept at 0 by construction.
    raw_protocol_drops_total: int = 0
    price_events_in: int = 0
    price_events_processed: int = 0
    price_queue_size: int = 0
    price_queue_blocked_total: int = 0
    late_price_events_total: int = 0
    engine_evaluations_total: int = 0
    history_lookups_total: int = 0
    history_gaps_total: int = 0
    history_backfills_total: int = 0
    rule_matches_total: int = 0
    signals_total: int = 0
    signals_correlated_total: int = 0
    persistence_drops_total: int = 0
    persistence_failures_total: int = 0
    notification_drops_total: int = 0
    subscribe_acks_total: int = 0
    subscribe_failures_total: int = 0
    errors_total: int = 0
    task_restarts_total: int = 0
    exchange_clock_skew_ms: float = 0.0
    event_loop_lag_ms: float = 0.0
    event_loop_lag_max_ms: float = 0.0
    last_message_age_ms: float | None = None
    ready: bool = False
    # Optional trading module (never exposes balances).
    trading_enabled: bool = False
    trading_ready: bool = False
    trade_signals_received_total: int = 0
    trade_signals_ignored_total: int = 0
    trade_signals_skipped_total: int = 0
    orders_submitted_total: int = 0
    orders_filled_total: int = 0
    orders_failed_total: int = 0
    open_positions: int = 0
    active_orders: int = 0
    last_private_api_success_ts: float | None = None
    rates: dict[str, float] = field(default_factory=dict)
    _latency: array = field(default_factory=lambda: array("d", [0.0] * _LATENCY_SAMPLES))
    _latency_n: int = 0
    _rate_prev: dict[str, int] = field(default_factory=dict)
    _rate_ts: float = field(default_factory=time.monotonic)

    def record_error(self) -> None:
        self.errors_total += 1

    def record_reconnect(self) -> None:
        self.ws_reconnects += 1

    def record_signal(self) -> None:
        self.signals_total += 1

    def record_latency(self, seconds: float) -> None:
        self._latency[self._latency_n % _LATENCY_SAMPLES] = seconds
        self._latency_n += 1

    def latency_percentiles_ms(self) -> dict[str, float | None]:
        n = min(self._latency_n, _LATENCY_SAMPLES)
        if n == 0:
            return {"p50": None, "p95": None, "p99": None}
        values = sorted(self._latency[:n])
        return {
            "p50": round(values[int(n * 0.50)] * 1000, 3),
            "p95": round(values[min(n - 1, int(n * 0.95))] * 1000, 3),
            "p99": round(values[min(n - 1, int(n * 0.99))] * 1000, 3),
        }

    def update_rates(self) -> None:
        now = time.monotonic()
        elapsed = now - self._rate_ts
        if elapsed <= 0:
            return
        for name in _RATE_FIELDS:
            value = getattr(self, name)
            prev = self._rate_prev.get(name, value)
            self.rates[name.removesuffix("_total") + "_per_sec"] = round(
                (value - prev) / elapsed, 2
            )
            self._rate_prev[name] = value
        self._rate_ts = now

    @property
    def lost_price_events(self) -> int:
        return self.price_events_in - self.price_events_processed - self.price_queue_size

    def snapshot(self) -> dict:
        counters = {
            k: v
            for k, v in vars(self).items()
            if not k.startswith("_") and k not in {"rates"}
        }
        return {
            **counters,
            "lost_price_events": self.lost_price_events,
            "pipeline_latency_ms": self.latency_percentiles_ms(),
            "rates": dict(self.rates),
            # Back-compat keys used by /status and older dashboards.
            "messages_per_sec": self.rates.get("ticker_messages_per_sec", 0.0),
            "messages_total": self.ticker_messages_total,
            "queue_size": self.price_queue_size,
            "queue_drops": self.raw_protocol_drops_total,
        }


metrics = Metrics()
