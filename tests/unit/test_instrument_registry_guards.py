import pytest

from screener.application.instrument_registry import InstrumentRegistry
from screener.domain.models import Instrument


def _inst(symbol: str) -> Instrument:
    return Instrument(
        symbol=symbol,
        contract_type="LinearPerpetual",
        status="Trading",
        settle_coin="USDT",
        base_coin=symbol.removesuffix("USDT"),
        quote_coin="USDT",
        symbol_type=None,
        is_pre_listing=False,
    )


class _Feed:
    def __init__(self, symbols: list[str]) -> None:
        self.symbols = symbols

    async def fetch(self) -> list[dict]:
        return [{"symbol": s} for s in self.symbols]


def _registry(feed: _Feed, on_change=None) -> InstrumentRegistry:
    return InstrumentRegistry(
        None,
        fetch=feed.fetch,
        accept=lambda raw: True,
        mapper=lambda raw: _inst(raw["symbol"]),
        on_change=on_change,
    )


@pytest.mark.asyncio
async def test_empty_response_keeps_universe() -> None:
    feed = _Feed(["AUSDT", "BUSDT", "CUSDT"])
    registry = _registry(feed)
    await registry.refresh()
    feed.symbols = []
    await registry.refresh()
    assert registry.symbols == ["AUSDT", "BUSDT", "CUSDT"]


@pytest.mark.asyncio
async def test_exclusions_are_not_treated_as_suspicious() -> None:
    feed = _Feed(["AUSDT", "BUSDT", "CUSDT"])
    registry = _registry(feed)
    await registry.refresh()
    registry.set_exclude_symbols({"AUSDT", "BUSDT"})
    await registry.refresh()
    assert registry.symbols == ["CUSDT"]


@pytest.mark.asyncio
async def test_failed_change_is_applied_again_on_next_refresh() -> None:
    feed = _Feed(["AUSDT", "BUSDT"])
    calls: list[set[str]] = []
    fail = {"on": False}

    async def on_change(instruments, added, removed) -> None:
        calls.append(added)
        if fail["on"]:
            raise ConnectionError("ws closed")

    registry = _registry(feed, on_change)
    await registry.refresh()
    feed.symbols = ["AUSDT", "BUSDT", "CUSDT"]
    fail["on"] = True
    with pytest.raises(ConnectionError):
        await registry.refresh()
    assert registry.symbols == ["AUSDT", "BUSDT"]
    fail["on"] = False
    await registry.refresh()
    assert calls[-1] == {"CUSDT"}
    assert registry.symbols == ["AUSDT", "BUSDT", "CUSDT"]
