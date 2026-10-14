from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime
from typing import Any

import orjson

# Levels used across the app:
#   DEBUG   — hot-path / verbose diagnostics (ticks, HTTP timing, queue sizes)
#   INFO    — lifecycle and business events (start, universe, signals, trades)
#   WARNING — recoverable issues (timeouts, reconnects, skips, task restart)
#   ERROR   — failed operations that need attention
#   CRITICAL — safety stops (trading queue overflow, protective SL failure)

_CONTEXT_KEYS = (
    "component",
    "event",
    "symbol",
    "signal_id",
    "signal_type",
    "action",
    "side",
    "reason",
    "mode",
    "status",
    "duration_ms",
    "count",
    "trade_id",
    "order_id",
    "order_link_id",
    "close_reason",
    "extra",
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        for key in _CONTEXT_KEYS:
            if hasattr(record, key):
                value = getattr(record, key)
                if value is not None:
                    payload[key] = value
        return orjson.dumps(payload).decode()


def setup_logging(*, level: str = "INFO", json_logs: bool = True) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level.upper())

    handler = logging.StreamHandler(sys.stdout)
    if json_logs:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s [%(name)s] %(message)s"
            )
        )
    root.addHandler(handler)

    # Quiet noisy libraries. Healthchecks flood access logs at INFO.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)
    logging.getLogger("aiogram").setLevel(logging.INFO)
    logging.getLogger("aiogram.event").setLevel(logging.INFO)

    logging.getLogger("screener").setLevel(level.upper())


def log(
    logger: logging.Logger,
    level: int,
    msg: str,
    *,
    component: str | None = None,
    event: str | None = None,
    symbol: str | None = None,
    exc_info: bool = False,
    **fields: Any,
) -> None:
    """Structured log with optional context fields for JSON formatter."""
    extra: dict[str, Any] = {}
    if component is not None:
        extra["component"] = component
    if event is not None:
        extra["event"] = event
    if symbol is not None:
        extra["symbol"] = symbol
    for key, value in fields.items():
        if value is not None and key in _CONTEXT_KEYS:
            extra[key] = value
        elif value is not None:
            # Unknown keys go into nested extra blob.
            blob = extra.setdefault("extra", {})
            if isinstance(blob, dict):
                blob[key] = value
    logger.log(level, msg, extra=extra, exc_info=exc_info)
