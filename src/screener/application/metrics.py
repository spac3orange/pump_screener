from __future__ import annotations

import time
from dataclasses import dataclass, field
from threading import Lock


@dataclass
class Metrics:
    instruments_active: int = 0
    ws_connections: int = 0
    ws_reconnects: int = 0
    messages_total: int = 0
    messages_per_sec: float = 0.0
    signals_total: int = 0
    errors_total: int = 0
    queue_size: int = 0
    queue_drops: int = 0
    processing_lag_ms: float = 0.0
    last_message_age_ms: float | None = None
    last_ws_message_ts: float | None = None
    ready: bool = False
    _msg_window_start: float = field(default_factory=time.monotonic, repr=False)
    _msg_window_count: int = field(default=0, repr=False)
    _lock: Lock = field(default_factory=Lock, repr=False)

    def record_message(self, *, received_ts: float | None = None) -> None:
        now = time.monotonic()
        with self._lock:
            self.messages_total += 1
            self._msg_window_count += 1
            elapsed = now - self._msg_window_start
            if elapsed >= 1.0:
                self.messages_per_sec = self._msg_window_count / elapsed
                self._msg_window_count = 0
                self._msg_window_start = now
            if received_ts is not None:
                self.last_ws_message_ts = received_ts
                self.last_message_age_ms = (time.time() - received_ts) * 1000

    def record_signal(self) -> None:
        with self._lock:
            self.signals_total += 1

    def record_error(self) -> None:
        with self._lock:
            self.errors_total += 1

    def record_reconnect(self) -> None:
        with self._lock:
            self.ws_reconnects += 1

    def record_drop(self, n: int = 1) -> None:
        with self._lock:
            self.queue_drops += n

    def snapshot(self) -> dict:
        with self._lock:
            age = None
            if self.last_ws_message_ts is not None:
                age = (time.time() - self.last_ws_message_ts) * 1000
            return {
                "instruments_active": self.instruments_active,
                "ws_connections": self.ws_connections,
                "ws_reconnects": self.ws_reconnects,
                "messages_total": self.messages_total,
                "messages_per_sec": round(self.messages_per_sec, 2),
                "signals_total": self.signals_total,
                "errors_total": self.errors_total,
                "queue_size": self.queue_size,
                "queue_drops": self.queue_drops,
                "processing_lag_ms": round(self.processing_lag_ms, 2),
                "last_message_age_ms": None if age is None else round(age, 2),
                "ready": self.ready,
            }


metrics = Metrics()
