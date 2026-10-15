#!/usr/bin/env python3
"""Read-only reconciliation of Telegram Stars income against the ``payments`` table.

Lists incoming Stars transactions (getStarTransactions) of the last N days whose
charge id is not recorded in PostgreSQL, i.e. payments Telegram took but the bot
never applied. Nothing is written anywhere; fix findings manually (/admin_refund
or a support conversation). Uses TELEGRAM_BOT_TOKEN and DATABASE_URL from env.

    python scripts/reconcile_stars.py --days 7
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from aiogram.types import TransactionPartnerUser

from screener.config import get_system_settings
from screener.infrastructure.db.commercial_repo import CommercialRepository
from screener.infrastructure.db.session import create_engine, create_session_factory

PAGE = 100


async def _incoming(bot: Bot, since: datetime) -> list[tuple[str, int, int, datetime]]:
    """``(charge_id, telegram_user_id, amount, date)`` of user payments since ``since``."""
    found: list[tuple[str, int, int, datetime]] = []
    offset = 0
    while True:
        page = await bot.get_star_transactions(offset=offset, limit=PAGE)
        if not page.transactions:
            break
        oldest = None
        for tx in page.transactions:
            date = (
                tx.date if isinstance(tx.date, datetime) else datetime.fromtimestamp(tx.date, UTC)
            )
            oldest = date if oldest is None else min(oldest, date)
            if date >= since and isinstance(tx.source, TransactionPartnerUser):
                found.append((tx.id, tx.source.user.id, tx.amount, date))
        if len(page.transactions) < PAGE or (oldest is not None and oldest < since):
            break
        offset += PAGE
    return found


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()
    system = get_system_settings()
    if not system.telegram_bot_token:
        print("TELEGRAM_BOT_TOKEN is not set", file=sys.stderr)
        return 2
    since = datetime.now(UTC) - timedelta(days=args.days)
    bot = Bot(system.telegram_bot_token)
    engine = create_engine(system.database_url)
    try:
        incoming = await _incoming(bot, since)
        known = await CommercialRepository(create_session_factory(engine)).applied_charge_ids(
            since - timedelta(days=1)
        )
    finally:
        await bot.session.close()
        await engine.dispose()
    missing = [tx for tx in incoming if tx[0] not in known]
    print(
        f"Stars payments since {since:%Y-%m-%d %H:%M} UTC: {len(incoming)}, "
        f"unrecorded: {len(missing)}"
    )
    for charge_id, user_id, amount, date in missing:
        print(
            f"MISSING charge={charge_id} tg_user={user_id} amount={amount} "
            f"date={date:%Y-%m-%d %H:%M}"
        )
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
