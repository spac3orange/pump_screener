"""Commercial bot end to end: aiogram dispatcher + real PostgreSQL + mocked Telegram API.

No network: every Bot API call is captured by ``MockSession``. Opt-in like the
other DB tests (TEST_DATABASE_URL must point to a *test* database).
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from aiogram.client.default import Default
from aiogram.methods import (
    AnswerCallbackQuery,
    AnswerPreCheckoutQuery,
    CreateInvoiceLink,
    EditUserStarSubscription,
    SendMessage,
    SetMyCommands,
)
from aiogram.types import (
    BotCommandScopeChat,
    PreCheckoutQuery,
    RefundedPayment,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    SuccessfulPayment,
    Update,
)
from sqlalchemy import text

from screener.app_state import AppState
from screener.application.clock import FakeClock
from screener.application.price_history import PriceHistoryManager
from screener.commercial_runtime import build_commercial
from screener.config import CommercialSettings, ScreenerSettings
from screener.domain.commercial import AccessKind, GroupSignal
from screener.domain.enums import Direction, HistoryQuality, SignalType
from screener.domain.models import RuleMatch
from screener.infrastructure.db.session import create_engine, create_session_factory
from screener.infrastructure.telegram import bot as bot_module
from screener.infrastructure.telegram.formatter import format_signal_message
from screener.infrastructure.telegram.user_keyboards import MENU_ACCOUNT, MENU_SUBSCRIPTION
from tests.telegram_fakes import (
    MockSession,
    callback_update,
    detach_routers,
    msg_update,
    next_id,
    tg_user,
)

URL = os.getenv("TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    "test" not in URL.rsplit("/", 1)[-1],
    reason="TEST_DATABASE_URL to a *test* database is required",
)

ADMIN = 1
ALICE = 1001
BOB = 1002
T0 = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


_msg = msg_update
_cb = callback_update
_user = tg_user


@pytest.fixture
async def env():
    engine = create_engine(URL)
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))

    def _upgrade() -> None:
        from alembic.config import Config

        from alembic import command

        cfg = Config("alembic.ini")
        cfg.set_main_option("sqlalchemy.url", URL)
        command.upgrade(cfg, "head")

    await asyncio.to_thread(_upgrade)
    sf = create_session_factory(engine)
    clock = FakeClock(int(T0.timestamp() * 1000))
    settings = CommercialSettings(
        _env_file=None,  # type: ignore[call-arg]
        commercial_enabled=True,
        subscription_price_stars=1500,
        user_chat_min_interval_sec=0.0,
        user_send_rate_per_sec=1000.0,
        user_rate_burst=100,
        support_contact="@support",
    )
    template = ScreenerSettings(min_turnover_24h_usdt=0)
    rt = build_commercial(
        settings=settings,
        session_factory=sf,
        history=PriceHistoryManager(),
        template_hint=template,
        format_signal=lambda s, n: format_signal_message(s, signals_24h=n),
        clock=clock,
    )
    await rt.users.bootstrap(template, source_profile_version_id=None, admin_ids=[ADMIN])
    trading = SimpleNamespace(enqueued=[], enqueue=lambda s: trading.enqueued.append(s))
    state = AppState(
        system=SimpleNamespace(admin_id=[ADMIN], is_admin=lambda uid: uid == ADMIN),  # type: ignore[arg-type]
        repo=None,  # type: ignore[arg-type]
        rest=None,  # type: ignore[arg-type]
        history=None,  # type: ignore[arg-type]
        processor=None,  # type: ignore[arg-type]
        signal_engine=SimpleNamespace(settings=template),  # type: ignore[arg-type]
        correlator=None,  # type: ignore[arg-type]
        registry=None,  # type: ignore[arg-type]
        ws_manager=None,  # type: ignore[arg-type]
        persistence=None,  # type: ignore[arg-type]
        notifications=None,  # type: ignore[arg-type]
        trading=trading,  # type: ignore[arg-type]
        commercial=rt,
    )
    state.feeds["binance"] = SimpleNamespace(evaluate_admin=True)
    detach_routers()
    svc = bot_module.TelegramBotService("123456:TEST-token", state)
    session = MockSession()
    svc.bot.session = session
    rt.dispatcher.set_send(svc.send_to_chat)
    rt.payments.set_gateway(svc.stars)
    rt.payments.set_alert(svc.broadcast_text)
    rt.expiry.set_notify(svc.notify_access_ended)

    async def feed(update: Update) -> None:
        await svc._dp.feed_update(svc.bot, update)

    yield SimpleNamespace(
        rt=rt, svc=svc, session=session, feed=feed, clock=clock, trading=trading, sf=sf
    )
    detach_routers()
    await engine.dispose()


async def _onboard(env, uid: int = ALICE) -> None:
    await env.feed(_msg(uid, "/start"))
    await env.feed(_cb(uid, "ua:terms"))


async def test_start_registers_and_requires_terms(env) -> None:
    await env.feed(_msg(ALICE, "/start"))
    user = await env.rt.repo.get_user_by_telegram(ALICE)
    assert user is not None and user.trial_started_at is None  # never auto-started
    assert "условия" in env.session.texts(ALICE)[-1]
    assert await env.rt.repo.get_user_config(user.id) is not None


async def test_admin_alerted_once_per_new_user(env) -> None:
    await env.feed(_msg(ADMIN, "/start"))
    await env.feed(_msg(ALICE, "/start"))
    await env.feed(_msg(ALICE, "/start"))
    await env.feed(_msg(BOB, "/help"))
    alerts = [t for t in env.session.texts(ADMIN) if "Новый пользователь" in t]
    assert len(alerts) == 2
    assert f"tg://user?id={ALICE}" in alerts[0] and f"<code>{BOB}</code>" in alerts[1]


async def test_trial_requires_terms_then_starts_once(env) -> None:
    await env.feed(_msg(ALICE, "/start"))
    await env.feed(_cb(ALICE, "ua:trial"))
    assert (await env.rt.repo.get_user_by_telegram(ALICE)).trial_started_at is None
    await env.feed(_cb(ALICE, "ua:terms"))
    await env.feed(_cb(ALICE, "ua:trial"))
    await env.feed(_cb(ALICE, "ua:trial"))
    user = await env.rt.repo.get_user_by_telegram(ALICE)
    assert user.trial_ends_at == T0 + timedelta(hours=24)
    assert env.rt.registry.get(user.id) is not None
    answers = [c.text for c in env.session.of(AnswerCallbackQuery) if c.text]
    assert "Пробный период уже был использован." in answers


async def test_non_admin_cannot_reach_admin_commands(env) -> None:
    await _onboard(env)
    for cmd in ("/status", "/trading", "/profiles", "/admin_stats", "/admin_refund x"):
        await env.feed(_msg(ALICE, cmd))
    texts = env.session.texts(ALICE)[-5:]
    assert all("Неизвестная команда" in t for t in texts)
    await env.feed(_cb(ALICE, "trading:on"))
    await env.feed(_cb(ALICE, "set:long:pct"))
    assert env.trading.enqueued == []
    assert env.session.texts(ALICE)[-1] == texts[-1]


async def test_no_reply_keyboard_and_old_buttons_remove_it(env) -> None:
    await _onboard(env)
    assert not any(
        isinstance(c.reply_markup, ReplyKeyboardMarkup) for c in env.session.of(SendMessage)
    )
    await env.feed(_msg(ALICE, MENU_ACCOUNT))
    removed = [
        c for c in env.session.of(SendMessage) if isinstance(c.reply_markup, ReplyKeyboardRemove)
    ]
    assert removed and removed[-1].chat_id == ALICE
    assert "Мой аккаунт" in env.session.texts(ALICE)[-1]
    await env.feed(_cb(ALICE, "us:s:long:pct"))
    await env.feed(_msg(ALICE, MENU_SUBSCRIPTION))  # abandons the pending edit
    assert "PumpScreener Pro" in env.session.texts(ALICE)[-1]
    await env.feed(_msg(ALICE, "7"))
    user = await env.rt.repo.get_user_by_telegram(ALICE)
    assert (await env.rt.users.get_config(user.id)).long_threshold_pct == 40.0


async def test_link_previews_disabled_by_default(env) -> None:
    await _onboard(env)
    sent = env.session.of(SendMessage)[-1]
    assert sent.link_preview_options == Default("link_preview")
    assert env.svc.bot.default["link_preview"].is_disabled is True


async def test_command_menu_scopes(env) -> None:
    await env.svc.setup_command_menu()
    calls = env.session.of(SetMyCommands)
    by_scope = {type(c.scope).__name__: [b.command for b in c.commands] for c in calls}
    assert "admin_stats" not in by_scope["BotCommandScopeDefault"]
    assert {"account", "subscription"} <= set(by_scope["BotCommandScopeDefault"])
    admin_call = next(c for c in calls if isinstance(c.scope, BotCommandScopeChat))
    assert admin_call.scope.chat_id == ADMIN
    assert "admin_stats" in [b.command for b in admin_call.commands]


async def test_admin_gets_admin_tools_and_subscriber_commands(env) -> None:
    await env.feed(_msg(ADMIN, "/admin_stats"))
    assert "подписчики" in env.session.texts(ADMIN)[-1]
    await env.feed(_msg(ADMIN, "/account"))
    assert "администратор" in env.session.texts(ADMIN)[-1]
    admin = await env.rt.repo.get_user_by_telegram(ADMIN)
    assert admin.monitoring_enabled is False  # commercial copies are opt-in for admins


async def test_user_settings_edit_and_validation(env) -> None:
    await _onboard(env)
    await env.feed(_cb(ALICE, "us:s:long:pct"))
    await env.feed(_msg(ALICE, "abc"))
    assert "Нужно число" in env.session.texts(ALICE)[-1]
    await env.feed(_msg(ALICE, "7,5"))
    await env.feed(_cb(ALICE, "us:s:turnover:value"))
    await env.feed(_msg(ALICE, "2m"))
    user = await env.rt.repo.get_user_by_telegram(ALICE)
    cfg = await env.rt.users.get_config(user.id)
    assert cfg.long_threshold_pct == 7.5 and cfg.min_turnover_24h_usdt == 2_000_000
    await env.feed(_cb(ALICE, "us:reset_ok"))
    assert (await env.rt.users.get_config(user.id)).long_threshold_pct == 40.0


async def test_user_cannot_disable_last_exchange(env) -> None:
    await _onboard(env)
    await env.feed(_cb(ALICE, "us:x:binance"))
    user = await env.rt.repo.get_user_by_telegram(ALICE)
    assert user.enabled_exchanges == frozenset({"bybit"})
    await env.feed(_cb(ALICE, "us:x:bybit"))
    user = await env.rt.repo.get_user_by_telegram(ALICE)
    assert user.enabled_exchanges == frozenset({"bybit"})
    alerts = [c.text for c in env.session.of(AnswerCallbackQuery) if c.text]
    assert any("последнюю биржу" in t for t in alerts)


async def test_admin_exchange_toggle_keeps_one_on(env) -> None:
    await env.feed(_msg(ADMIN, "/settings"))
    await env.feed(_cb(ADMIN, "ex:binance"))
    assert env.svc._app_state.admin_exchanges == {"bybit"}
    await env.feed(_cb(ADMIN, "ex:bybit"))
    assert env.svc._app_state.admin_exchanges == {"bybit"}
    alerts = [c.text for c in env.session.of(AnswerCallbackQuery) if c.text]
    assert any("последнюю биржу" in t for t in alerts)


async def _buy(env, uid: int = ALICE) -> str:
    await env.feed(_cb(uid, "ua:buy"))
    link_calls = env.session.of(CreateInvoiceLink)
    assert link_calls, "invoice link was not requested"
    call = link_calls[-1]
    assert call.currency == "XTR" and call.prices[0].amount == 1500
    assert call.subscription_period == 2_592_000
    assert call.payload.startswith("ps1_")
    return call.payload


def _paid(
    uid: int, payload: str, charge: str, *, first: bool = True, exp=None, amount: int = 1500
) -> Update:
    return _msg(
        uid,
        successful_payment=SuccessfulPayment(
            currency="XTR",
            total_amount=amount,
            invoice_payload=payload,
            telegram_payment_charge_id=charge,
            provider_payment_charge_id="",
            is_recurring=True,
            is_first_recurring=first,
            subscription_expiration_date=None if exp is None else int(exp.timestamp()),
        ),
    )


async def test_full_payment_lifecycle(env) -> None:
    await _onboard(env)
    payload = await _buy(env)

    await env.feed(
        Update(
            update_id=next_id(),
            pre_checkout_query=PreCheckoutQuery(
                id="pc1",
                from_user=_user(BOB),
                currency="XTR",
                total_amount=1500,
                invoice_payload=payload,
            ),
        )
    )
    assert env.session.of(AnswerPreCheckoutQuery)[-1].ok is False  # someone else's invoice

    await env.feed(
        Update(
            update_id=next_id(),
            pre_checkout_query=PreCheckoutQuery(
                id="pc2",
                from_user=_user(ALICE),
                currency="XTR",
                total_amount=1500,
                invoice_payload=payload,
            ),
        )
    )
    assert env.session.of(AnswerPreCheckoutQuery)[-1].ok is True
    user = await env.rt.repo.get_user_by_telegram(ALICE)
    assert user.access_until is None  # pre-checkout never grants access

    await env.feed(_paid(ALICE, payload, "ch1"))
    await env.feed(_paid(ALICE, payload, "ch1"))  # Telegram duplicate
    user = await env.rt.repo.get_user_by_telegram(ALICE)
    view = await env.rt.users.account_view(user)
    assert view.access.kind is AccessKind.PRO
    assert sum("Оплата получена" in t for t in env.session.texts(ALICE)) == 1
    assert env.rt.registry.get(user.id) is not None

    await env.feed(_cb(ALICE, "ua:cancel_ok"))
    edit = env.session.of(EditUserStarSubscription)[-1]
    assert edit.is_canceled is True and edit.telegram_payment_charge_id == "ch1"
    sub = await env.rt.repo.get_live_subscription(user.id)
    assert sub.cancel_at_period_end is True
    await env.feed(_cb(ALICE, "ua:resume"))
    assert (await env.rt.repo.get_live_subscription(user.id)).cancel_at_period_end is False

    renew_exp = sub.current_period_end + timedelta(days=30)
    await env.feed(_paid(ALICE, payload, "ch2", first=False, exp=renew_exp))
    assert (await env.rt.repo.get_live_subscription(user.id)).current_period_end == renew_exp

    await env.feed(
        _msg(
            ALICE,
            refunded_payment=RefundedPayment(
                currency="XTR",
                total_amount=1500,
                invoice_payload=payload,
                telegram_payment_charge_id="ch2",
            ),
        )
    )
    user = await env.rt.repo.get_user_by_telegram(ALICE)
    assert (await env.rt.users.account_view(user)).access.kind is AccessKind.NONE
    assert env.rt.registry.get(user.id) is None


async def test_payment_from_wrong_amount_is_anomaly_and_alerts_admin(env) -> None:
    await _onboard(env)
    payload = await _buy(env)
    await env.feed(_paid(ALICE, payload, "chX", amount=1))
    user = await env.rt.repo.get_user_by_telegram(ALICE)
    assert user.access_until is None
    assert any("anomaly" in t for t in env.session.texts(ADMIN))


def _signal(group_key: str, symbol: str = "AUSDT") -> GroupSignal:
    m = RuleMatch(
        SignalType.PUMP, 720, 40.0, 45.0, 1.0, 1.45, int(T0.timestamp() * 1000), HistoryQuality.LIVE
    )
    return GroupSignal(group_key, symbol, Direction.UP, m, (m,), T0)


async def test_commercial_signal_reaches_subscriber_never_trading(env) -> None:
    await _onboard(env)
    await env.feed(_cb(ALICE, "ua:trial"))
    user = await env.rt.repo.get_user_by_telegram(ALICE)
    entry = env.rt.registry.get(user.id)
    env.rt.dispatcher.on_group_signal(_signal(entry.group_key))
    env.rt.dispatcher.fanout_once()
    await env.rt.dispatcher.send_next()
    last = env.session.texts(ALICE)[-1]
    assert "Pump" in last and "PumpScreener Pro" in last
    assert env.trading.enqueued == []
    await env.rt.writer.flush_once()
    async with env.sf() as s:
        n = (await s.execute(text("SELECT count(*) FROM user_signal_events"))).scalar_one()
    assert n == 1


async def test_expiry_notifies_once_and_stops_delivery(env) -> None:
    await _onboard(env)
    await env.feed(_cb(ALICE, "ua:trial"))
    user = await env.rt.repo.get_user_by_telegram(ALICE)
    group = env.rt.registry.get(user.id).group_key
    env.clock.advance(24 * 3600 * 1000)
    env.rt.dispatcher.on_group_signal(_signal(group, "BUSDT"))
    env.rt.dispatcher.fanout_once()
    await env.rt.dispatcher.send_next()
    assert not any("BUSDT" in t or ">B<" in t for t in env.session.texts(ALICE))
    assert await env.rt.expiry.run_once() == 1
    assert await env.rt.expiry.run_once() == 0
    ended = [t for t in env.session.texts(ALICE) if "Пробный период закончился" in t]
    assert len(ended) == 1
    assert env.rt.registry.get(user.id) is None


async def test_blocked_user_is_marked_and_unblocked_on_return(env) -> None:
    from aiogram.exceptions import TelegramForbiddenError

    await _onboard(env)
    await env.feed(_cb(ALICE, "ua:trial"))
    user = await env.rt.repo.get_user_by_telegram(ALICE)
    group = env.rt.registry.get(user.id).group_key

    async def forbidden(bot, method, timeout=None):
        raise TelegramForbiddenError(
            method=method, message="Forbidden: bot was blocked by the user"
        )

    original = env.session.make_request
    env.session.make_request = forbidden  # type: ignore[method-assign]
    env.rt.dispatcher.on_group_signal(_signal(group))
    env.rt.dispatcher.fanout_once()
    await env.rt.dispatcher.send_next()
    await asyncio.sleep(0.05)
    env.session.make_request = original  # type: ignore[method-assign]
    assert (await env.rt.repo.get_user(user.id)).status.value == "bot_blocked"
    assert env.rt.registry.get(user.id) is None
    await env.feed(_msg(ALICE, "/account"))
    assert (await env.rt.repo.get_user(user.id)).status.value == "active"
    assert env.rt.registry.get(user.id) is not None
