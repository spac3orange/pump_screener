from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from screener.app_state import AppState
from screener.application.correlator import SignalCorrelator
from screener.application.dedup import DedupKey
from screener.application.profiles import ActiveProfile, ProfileService
from screener.config import MONITORING_CONFIG_SCHEMA_VERSION, ScreenerSettings
from screener.domain.enums import Direction, HistoryQuality, PriceEventKind, SignalType
from screener.domain.models import PriceEvent, RuleMatch, SignalContext
from screener.domain.profiles import (
    DEFAULT_PROFILE_NAME,
    MonitoringProfile,
    ProfileNameTaken,
    ProfileNotFound,
    ProfileProtected,
    ProfileValidationError,
    ProfileVersion,
    normalize_profile_name,
)
from screener.infrastructure.telegram import handlers
from screener.infrastructure.telegram.profiles_view import (
    profile_card_keyboard,
    profiles_list_keyboard,
    profiles_list_text,
    rules_summary,
)
from tests.helpers import T0, Harness, settings


class FakeStore:
    """In-memory ProfileStore with the same invariants as the PostgreSQL repository."""

    def __init__(self) -> None:
        self.rows: dict[UUID, dict[str, Any]] = {}
        self.versions: dict[UUID, ProfileVersion] = {}
        self.active_id: UUID | None = None
        self.yield_in_writes = False

    async def _tick(self) -> None:
        if self.yield_in_writes:
            await asyncio.sleep(0)

    def _profile(self, pid: UUID) -> MonitoringProfile:
        row = self.rows[pid]
        return MonitoringProfile(
            id=pid,
            name=row["name"],
            description=row["description"],
            is_system=row["is_system"],
            current=self.versions[row["current"]],
        )

    def _live(self) -> list[UUID]:
        return [pid for pid, row in self.rows.items() if not row["deleted"]]

    def _check_name(self, name: str, *, exclude: UUID | None = None) -> None:
        for pid in self._live():
            if pid != exclude and self.rows[pid]["name"].lower() == name.lower():
                raise ProfileNameTaken(f"Профиль «{name}» уже существует.")

    def _new_version(self, pid: UUID, config: dict, schema: int, config_hash: str) -> UUID:
        numbers = [v.version_no for v in self.versions.values() if v.profile_id == pid]
        version = ProfileVersion(
            id=uuid4(),
            profile_id=pid,
            version_no=max(numbers, default=0) + 1,
            config=dict(config),
            config_schema_version=schema,
            config_hash=config_hash,
        )
        self.versions[version.id] = version
        return version.id

    async def list_profiles(self) -> list[MonitoringProfile]:
        return sorted((self._profile(pid) for pid in self._live()), key=lambda p: p.name.lower())

    async def get_profile(self, profile_id: UUID) -> MonitoringProfile | None:
        row = self.rows.get(profile_id)
        if row is None or row["deleted"]:
            return None
        return self._profile(profile_id)

    async def get_active_profile_id(self) -> UUID | None:
        return self.active_id

    async def create_profile(
        self,
        *,
        name: str,
        description: str | None,
        config: dict[str, Any],
        config_schema_version: int,
        config_hash: str,
        chat_id: int | None,
        is_system: bool = False,
    ) -> MonitoringProfile:
        await self._tick()
        self._check_name(name)
        pid = uuid4()
        self.rows[pid] = {
            "name": name,
            "description": description,
            "is_system": is_system,
            "deleted": False,
        }
        self.rows[pid]["current"] = self._new_version(
            pid, config, config_schema_version, config_hash
        )
        return self._profile(pid)

    async def add_version(
        self,
        profile_id: UUID,
        *,
        config: dict[str, Any],
        config_schema_version: int,
        config_hash: str,
        chat_id: int | None,
    ) -> MonitoringProfile:
        await self._tick()
        self.rows[profile_id]["current"] = self._new_version(
            profile_id, config, config_schema_version, config_hash
        )
        return self._profile(profile_id)

    async def rename(self, profile_id: UUID, name: str) -> None:
        await self._tick()
        self._check_name(name, exclude=profile_id)
        self.rows[profile_id]["name"] = name

    async def soft_delete(self, profile_id: UUID) -> None:
        await self._tick()
        if profile_id == self.active_id:
            raise ProfileProtected("active")
        self.rows[profile_id]["deleted"] = True

    async def set_active(self, profile_id: UUID, *, chat_id: int | None) -> None:
        await self._tick()
        self.active_id = profile_id

    def corrupt(self, profile_id: UUID) -> None:
        vid = self.rows[profile_id]["current"]
        version = self.versions[vid]
        broken = {**version.config, "long_threshold_pct": "not-a-number"}
        self.versions[vid] = replace(version, config=broken)


LEGACY = settings(
    long_threshold_pct=18.0,
    long_interval_sec=600,
    enable_short=False,
    short_threshold_pct=48.0,
    dump_threshold_pct=15.0,
    dump_interval_sec=600,
    exclude_symbols={"ZZZUSDT", "AAAUSDT"},
)


def _loader(value: ScreenerSettings = LEGACY):
    async def load() -> ScreenerSettings:
        return value

    return load


class Events:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> None:
        self.items.append(kwargs)

    def types(self) -> list[str]:
        return [item["event_type"] for item in self.items]


async def _service(store: FakeStore | None = None, **kwargs: Any) -> ProfileService:
    service = ProfileService(store or FakeStore(), **kwargs)
    await service.bootstrap(_loader())
    return service


# --- migration / bootstrap -----------------------------------------------------


async def test_bootstrap_migrates_legacy_settings_to_default_unchanged() -> None:
    store = FakeStore()
    events = Events()
    service = ProfileService(store, event_log=events)
    active = await service.bootstrap(_loader())

    assert active.name == DEFAULT_PROFILE_NAME
    assert active.profile is not None and active.profile.is_system
    assert active.settings == LEGACY
    assert active.settings.config_hash() == LEGACY.config_hash()
    assert active.profile.current.config_schema_version == MONITORING_CONFIG_SCHEMA_VERSION
    assert store.active_id == active.profile.id
    assert events.types() == ["monitoring_profiles_migrated"]


async def test_config_hash_is_order_independent() -> None:
    a = settings(exclude_symbols={"A", "B", "C"})
    b = settings(exclude_symbols={"C", "A", "B"})
    assert a.canonical_config() == b.canonical_config()
    assert a.config_hash() == b.config_hash()
    assert ScreenerSettings.from_profile_config(
        a.canonical_config(), MONITORING_CONFIG_SCHEMA_VERSION
    ) == a


async def test_profile_config_v1_adds_symbol_daily_limits() -> None:
    v1 = {k: v for k, v in LEGACY.canonical_config().items() if k != "symbol_daily_limits"}
    loaded = ScreenerSettings.from_profile_config(v1, 1)
    assert loaded.symbol_daily_limits == {}
    assert loaded.long_threshold_pct == LEGACY.long_threshold_pct


async def test_profile_config_rejects_unknown_or_missing_fields() -> None:
    config = LEGACY.canonical_config()
    with pytest.raises(ValueError):
        ScreenerSettings.from_profile_config(
            {**config, "telegram_bot_token": "x"}, MONITORING_CONFIG_SCHEMA_VERSION
        )
    missing = dict(config)
    missing.pop("long_threshold_pct")
    with pytest.raises(ValueError):
        ScreenerSettings.from_profile_config(missing, MONITORING_CONFIG_SCHEMA_VERSION)
    with pytest.raises(ValueError):
        ScreenerSettings.from_profile_config(config, MONITORING_CONFIG_SCHEMA_VERSION + 1)


async def test_active_profile_survives_restart() -> None:
    store = FakeStore()
    first = await _service(store)
    fast = await first.clone(first.active.profile.id, "Fast")
    await first.activate(fast.id)
    await first.update_active_settings(replace_settings(first.active.settings, 12.0))

    restarted = ProfileService(store)
    active = await restarted.bootstrap(_loader(settings()))
    assert active.name == "Fast"
    assert active.settings.long_threshold_pct == 12.0
    assert active.profile.current.version_no == 2


async def test_bootstrap_falls_back_to_default_when_active_is_invalid() -> None:
    store = FakeStore()
    service = await _service(store)
    other = await service.clone(service.active.profile.id, "Broken")
    await service.activate(other.id)
    store.corrupt(other.id)

    active = await ProfileService(store).bootstrap(_loader(settings()))
    assert active.name == DEFAULT_PROFILE_NAME
    assert store.active_id == active.profile.id


def replace_settings(base: ScreenerSettings, long_pct: float) -> ScreenerSettings:
    return base.model_copy(update={"long_threshold_pct": long_pct})


# --- CRUD -------------------------------------------------------------------------


async def test_create_from_current_and_duplicate_name_case_insensitive() -> None:
    service = await _service()
    profile = await service.create_from_current("  Night   mode ", description="-")
    assert profile.name == "Night mode"
    assert profile.description is None
    assert profile.current.config_hash == LEGACY.config_hash()
    with pytest.raises(ProfileNameTaken):
        await service.create_from_current("NIGHT MODE")
    with pytest.raises(ProfileNameTaken):
        await service.create_from_current("default")


@pytest.mark.parametrize("bad", ["", "   ", "x" * 65, "<b>x</b>", "a&b", "a\x00b", "a\u200eb"])
def test_profile_name_validation(bad: str) -> None:
    with pytest.raises(ProfileValidationError):
        normalize_profile_name(bad)


async def test_clone_copies_config_without_activating() -> None:
    service = await _service()
    source = service.active.profile
    clone = await service.clone(source.id, "Copy")
    assert clone.id != source.id
    assert clone.current.config == source.current.config
    assert clone.current.version_no == 1
    assert not clone.is_system
    assert service.active.profile.id == source.id


async def test_rename_active_profile_updates_runtime_name_without_dedup_reset() -> None:
    calls: list[tuple[str, bool]] = []
    service = await _service(apply=lambda active, reset: calls.append((active.name, reset)))
    calls.clear()
    pid = service.active.profile.id
    renamed = await service.rename(pid, "Main")
    assert renamed.name == "Main"
    assert service.active.name == "Main"
    assert calls == [("Main", False)]
    assert service.active.context.profile_name == "Main"


async def test_rename_conflict_keeps_old_name() -> None:
    service = await _service()
    other = await service.create_from_current("Other")
    with pytest.raises(ProfileNameTaken):
        await service.rename(other.id, "DEFAULT")
    assert (await service.get_profile(other.id)).name == "Other"


async def test_delete_rules() -> None:
    store = FakeStore()
    events = Events()
    service = await _service(store, event_log=events)
    default_id = service.active.profile.id
    spare = await service.create_from_current("Spare")
    current = await service.create_from_current("Current")
    await service.activate(current.id)

    with pytest.raises(ProfileProtected):
        await service.delete(default_id)
    with pytest.raises(ProfileProtected):
        await service.delete(current.id)

    await service.delete(spare.id)
    assert [p.name for p in await service.list_profiles()] == ["Current", "Default"]
    with pytest.raises(ProfileNotFound):
        await service.get_profile(spare.id)
    # The version stays for signal provenance; the name is free again.
    assert any(v.profile_id == spare.id for v in store.versions.values())
    await service.create_from_current("spare")
    assert "monitoring_profile_deleted" in events.types()


# --- activation / settings ---------------------------------------------------------


async def test_activate_applies_immediately_and_logs_event() -> None:
    applied: list[tuple[ActiveProfile, bool]] = []
    events = Events()
    service = await _service(
        apply=lambda active, reset: applied.append((active, reset)), event_log=events
    )
    fast = await service.clone(service.active.profile.id, "Fast")
    applied.clear()

    active = await service.activate(fast.id, chat_id=7)
    assert service.active is active
    assert applied == [(active, True)]
    changed = [e for e in events.items if e["event_type"] == "monitoring_profile_changed"]
    assert len(changed) == 1
    details = changed[0]["details"]
    assert details["to_profile_id"] == str(fast.id)
    assert details["to_version_id"] == str(fast.current.id)
    assert details["chat_id"] == 7


async def test_invalid_profile_does_not_replace_active() -> None:
    store = FakeStore()
    applied: list[bool] = []
    service = await _service(store, apply=lambda active, reset: applied.append(reset))
    before = service.active
    broken = await service.clone(before.profile.id, "Broken")
    store.corrupt(broken.id)
    applied.clear()

    with pytest.raises(ProfileValidationError):
        await service.activate(broken.id)
    assert service.active is before
    assert store.active_id == before.profile.id
    assert applied == []


async def test_failed_persist_does_not_replace_active() -> None:
    store = FakeStore()
    service = await _service(store)
    before = service.active
    other = await service.create_from_current("Other")

    async def boom(profile_id: UUID, *, chat_id: int | None) -> None:
        raise RuntimeError("db down")

    store.set_active = boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await service.activate(other.id)
    assert service.active is before


async def test_settings_edit_appends_immutable_version_to_active_profile() -> None:
    store = FakeStore()
    applied: list[bool] = []
    service = await _service(store, apply=lambda active, reset: applied.append(reset))
    v1 = service.active.profile.current
    applied.clear()

    active = await service.update_active_settings(replace_settings(service.active.settings, 25))
    assert active.profile.current.version_no == 2
    assert active.profile.id == v1.profile_id
    assert active.settings.long_threshold_pct == 25
    assert store.versions[v1.id].config == v1.config  # old version untouched
    assert applied == [False]

    same = await service.update_active_settings(active.settings)
    assert same is active  # identical hash: no new version
    assert len([v for v in store.versions.values() if v.profile_id == v1.profile_id]) == 2


# --- engine wiring -------------------------------------------------------------------


def _wire(h: Harness, service: ProfileService) -> SimpleNamespace:
    excluded: list[set[str]] = []
    state = SimpleNamespace(
        signal_engine=h.engine,
        correlator=h.correlator,
        registry=SimpleNamespace(set_exclude_symbols=lambda s: excluded.append(set(s))),
        profiles=service,
        excluded=excluded,
    )
    service.set_apply(lambda active, reset: AppState.apply_profile(state, active, reset))  # type: ignore[arg-type]
    AppState.apply_profile(state, service.active, False)  # type: ignore[arg-type]
    return state


async def _pump_profiles() -> tuple[Harness, ProfileService, MonitoringProfile, MonitoringProfile]:
    h = Harness()
    service = ProfileService(FakeStore())
    await service.bootstrap(_loader(settings(long_threshold_pct=40.0, long_interval_sec=60)))
    _wire(h, service)
    slow = service.active.profile
    fast_settings = settings(long_threshold_pct=10.0, long_interval_sec=60)
    fast = await service.clone(slow.id, "Fast")
    await service.activate(fast.id)
    await service.update_active_settings(fast_settings)
    fast = service.active.profile
    await service.activate(slow.id)
    return h, service, slow, fast


async def test_activation_swaps_engine_resets_dedup_and_keeps_history() -> None:
    h, service, slow, fast = await _pump_profiles()
    assert h.engine.settings.long_threshold_pct == 40.0
    assert h.engine.context.profile_version_id == slow.current.id

    await h.send("AUSDT", "snapshot", T0, lastPrice="1.0", turnover24h="9000000")
    await h.send("AUSDT", "delta", T0 + 60_000, lastPrice="1.2")
    await h.drain()
    assert h.signals == []  # 20% < 40%
    h.engine.dedup.evaluate(
        DedupKey("AUSDT", SignalType.PUMP, 720),
        change_pct=50.0,
        threshold_pct=40.0,
        now_ms=T0,
        triggered=True,
    )
    assert h.engine.dedup.active_count == 1
    points_before = h.history.lookup("AUSDT", now_ms=T0 + 60_000, interval_sec=60)

    await service.activate(fast.id)
    assert h.engine.settings.long_threshold_pct == 10.0
    assert h.engine.dedup.active_count == 0
    assert h.history.lookup("AUSDT", now_ms=T0 + 60_000, interval_sec=60) == points_before

    await h.send("AUSDT", "delta", T0 + 61_000, lastPrice="1.21")
    await h.drain()
    assert len(h.signals) == 1
    signal = h.signals[0]
    assert signal.monitoring_profile_id == fast.id
    assert signal.monitoring_profile_version_id == fast.current.id
    assert signal.monitoring_profile_name == "Fast"
    assert signal.monitoring_profile_version_no == 2
    assert signal.rule_version == fast.current.config_hash[:12]
    payload = signal.to_payload()["monitoring_profile"]
    assert payload["version_id"] == str(fast.current.id)
    assert payload["config_hash"] == fast.current.config_hash[:12]


async def test_activation_updates_registry_excludes_and_correlation_window() -> None:
    h = Harness()
    service = ProfileService(FakeStore())
    await service.bootstrap(_loader(settings(exclude_symbols={"XUSDT"})))
    state = _wire(h, service)
    other = await service.clone(service.active.profile.id, "Other")
    await service.activate(other.id)
    await service.update_active_settings(
        settings(exclude_symbols={"YUSDT"}, correlation_window_ms=250)
    )
    assert state.excluded[-1] == {"YUSDT"}
    assert h.correlator.window_ms == 250


async def test_concurrent_activation_never_mixes_configs() -> None:
    """Every signal's thresholds match exactly the profile version stamped on it."""
    h, service, slow, fast = await _pump_profiles()
    service._store.yield_in_writes = True  # type: ignore[attr-defined]
    thresholds = {slow.current.id: 40.0, fast.current.id: 10.0}
    symbols = [f"C{i:02d}USDT" for i in range(40)]
    for sym in symbols:
        await h.send(sym, "snapshot", T0, lastPrice="1.0", turnover24h="9000000")

    done = asyncio.Event()

    async def feed() -> None:
        try:
            await _feed()
        finally:
            done.set()

    async def _feed() -> None:
        for step in range(1, 120):
            for i, sym in enumerate(symbols):
                # 60 s change spans ~13%..230% across symbols: both thresholds are hit.
                price = (1 + 0.002 * (i % 10 + 1)) ** step
                await h.send(sym, "delta", T0 + step * 1000, lastPrice=f"{price:.4f}")
                if i % 5 == 0:
                    h.worker.process_event(h.worker.queue.get_nowait())
                    h.worker.queue.task_done()
                    await asyncio.sleep(0)
            await h.drain()

    async def toggle() -> None:
        n = 0
        while not done.is_set():
            await service.activate(fast.id if n % 2 == 0 else slow.id)
            n += 1
            await asyncio.sleep(0)

    await asyncio.gather(feed(), toggle())
    stamped = {s.monitoring_profile_version_id for s in h.signals}
    assert stamped == set(thresholds), "expected signals under both profiles"
    for signal in h.signals:
        expected = thresholds[signal.monitoring_profile_version_id]
        assert all(m.threshold_pct == expected for m in signal.matches)
        assert signal.primary.change_pct >= expected
        version = slow.current if expected == 40.0 else fast.current
        assert signal.rule_version == version.config_hash[:12]


async def test_correlator_never_groups_matches_from_different_contexts() -> None:
    out = []
    corr = SignalCorrelator(window_ms=10_000, rule_version=lambda: "x", on_signal=out.append)
    event = PriceEvent(
        symbol="AUSDT",
        last_price=1.5,
        exchange_ts_ms=T0,
        received_mono=0.0,
        kind=PriceEventKind.LIVE,
        turnover_24h=1.0,
        volume_24h=1.0,
    )

    def match(interval: int, threshold: float) -> RuleMatch:
        return RuleMatch(
            signal_type=SignalType.PUMP,
            interval_sec=interval,
            threshold_pct=threshold,
            change_pct=50.0,
            prev_price=1.0,
            price=1.5,
            detected_ts_ms=T0,
            history_quality=HistoryQuality.LIVE,
        )

    a = SignalContext(rule_version="aaa", profile_name="A")
    b = SignalContext(rule_version="bbb", profile_name="B")
    corr.add(event, [match(60, 40.0)], a)
    corr.add(event, [match(300, 10.0)], b)
    corr.flush_all()
    assert [(s.monitoring_profile_name, s.rule_version) for s in out] == [
        ("A", "aaa"),
        ("B", "bbb"),
    ]
    assert all(s.direction is Direction.UP and len(s.matches) == 1 for s in out)


# --- Telegram view / handlers -----------------------------------------------------------


async def test_profiles_view_escapes_names_and_protects_buttons() -> None:
    service = await _service()
    default = service.active.profile
    store = service._store  # type: ignore[attr-defined]
    other = await service.create_from_current("Night")
    store.rows[other.id]["name"] = "N<i>"  # bypass validation to check escaping
    profiles = await service.list_profiles()

    text = profiles_list_text(profiles, default.id)
    assert "N&lt;i&gt;" in text and "N<i>" not in text

    data = [b.callback_data for row in profiles_list_keyboard(profiles, default.id).inline_keyboard
            for b in row]
    assert f"prof:open:{other.id}" in data and "prof:new" in data
    assert all(len(d.encode()) <= 64 for d in data)

    def actions(profile: MonitoringProfile, active: bool) -> list[str]:
        kb = profile_card_keyboard(profile, active=active)
        return [b.callback_data.split(":")[1] for row in kb.inline_keyboard for b in row]

    assert "del" not in actions(default, True)
    assert "act" not in actions(default, True)
    other_profile = await service.get_profile(other.id)
    assert "del" in actions(other_profile, False) and "act" in actions(other_profile, False)
    assert "del" not in actions(replace(other_profile, is_system=True), False)


def test_rules_summary_shows_off_rules() -> None:
    lines = rules_summary(
        settings(
            enable_dump=False,
            long_threshold_pct=33,
            long_interval_sec=420,
            symbol_daily_limits={"BTCUSDT": 2},
        )
    )
    assert "PUMP: 33% / 7m" in lines
    assert any(line.startswith("DUMP: OFF") for line in lines)
    assert "Daily caps: BTCUSDT:2" in lines


class _Query:
    def __init__(self, data: str) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=1)
        self.message = None
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))


async def test_activation_with_trading_on_requires_confirmation() -> None:
    service = await _service()
    other = await service.create_from_current("Other")
    state = SimpleNamespace(profiles=service, trading=SimpleNamespace(enabled=True))

    await handlers.on_profile_action(_Query(f"prof:act:{other.id}"), state)  # type: ignore[arg-type]
    assert service.active.name == DEFAULT_PROFILE_NAME

    await handlers.on_profile_action(_Query(f"prof:act_ok:{other.id}"), state)  # type: ignore[arg-type]
    assert service.active.name == "Other"
    assert state.trading.enabled is True  # trading is never switched off by a profile change


async def test_activation_with_trading_off_is_immediate() -> None:
    service = await _service()
    other = await service.create_from_current("Other")
    state = SimpleNamespace(profiles=service, trading=None)
    await handlers.on_profile_action(_Query(f"prof:act:{other.id}"), state)  # type: ignore[arg-type]
    assert service.active.name == "Other"


async def test_profile_errors_are_reported_not_raised() -> None:
    service = await _service()
    state = SimpleNamespace(profiles=service, trading=None)
    query = _Query(f"prof:del_ok:{service.active.profile.id}")
    await handlers.on_profile_action(query, state)  # type: ignore[arg-type]
    assert query.answers and query.answers[-1][1] is True
    bad = _Query("prof:open:not-a-uuid")
    await handlers.on_profile_action(bad, state)  # type: ignore[arg-type]
    assert bad.answers[-1][1] is True
