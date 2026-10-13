from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from screener.domain.models import Instrument, MarketSignal


class InstrumentSource(Protocol):
    async def fetch_linear_instruments(self) -> list[dict]: ...


class NotifierPort(Protocol):
    async def send_signal(
        self, chat_id: int, signal: MarketSignal, signals_24h: int = 1
    ) -> None: ...


class SignalSink(Protocol):
    async def save_signal(self, signal: MarketSignal) -> None: ...


class SettingsStore(Protocol):
    async def load_screener_settings(self) -> dict | None: ...

    async def save_screener_settings(self, config: dict, *, chat_id: int | None) -> None: ...


class InstrumentSink(Protocol):
    async def upsert_instruments(self, instruments: Sequence[Instrument]) -> None: ...
