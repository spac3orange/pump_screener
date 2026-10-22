from types import SimpleNamespace

from screener.config import CommercialSettings
from screener.domain.commercial import AccessKind, UserDetectionConfig
from screener.infrastructure.telegram.user_keyboards import (
    pay_keyboard,
    start_keyboard,
    subscription_keyboard,
    user_settings_keyboard,
)


def _view() -> SimpleNamespace:
    return SimpleNamespace(
        trial_available=True,
        access=SimpleNamespace(active=False, kind=AccessKind.NONE),
        subscription=None,
    )


def test_trial_and_subscription_buttons_are_green() -> None:
    settings = CommercialSettings(_env_file=None, subscription_price_stars=1500)
    start = start_keyboard(_view())
    assert start.inline_keyboard[0][0].text == "🎁 Начать пробный период"
    assert start.inline_keyboard[0][0].style == "success"
    assert start.inline_keyboard[1][0].text == "⭐ Оформить подписку"
    assert start.inline_keyboard[1][0].style == "success"

    sub = subscription_keyboard(_view(), settings)
    assert sub.inline_keyboard[0][0].style == "success"
    assert sub.inline_keyboard[1][0].style == "success"

    pay = pay_keyboard("https://t.me/invoice", settings)
    assert pay.inline_keyboard[0][0].style == "success"


def test_reset_button_is_red() -> None:
    reset = user_settings_keyboard(UserDetectionConfig()).inline_keyboard[-1][0]
    assert reset.text == "↩️ Сбросить по умолчанию"
    assert reset.style == "danger"
