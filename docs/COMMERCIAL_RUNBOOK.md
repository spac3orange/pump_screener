# Commercial MVP runbook

Commercial mode (`COMMERCIAL_ENABLED=true`) adds subscribers, a one-time 24 h trial and
PumpScreener Pro paid in Telegram Stars. It never touches auto-trading: commercial signals go only
to `UserDispatcher`, and the admin pipeline (`SignalEngine` → correlator → trading) is unchanged.

## 1. Staging first

Staging has its own PostgreSQL volume, its **own BotFather bot** and trading forced off.

```bash
cp .env.example .env.staging     # set TELEGRAM_BOT_TOKEN of the staging bot, ADMIN_ID, COMMERCIAL_ENABLED=true
docker compose -p pump_screener_staging -f docker-compose.staging.yml --env-file .env.staging up -d --build
```

Never put the production bot token or Bybit keys into `.env.staging`.

Smoke test in the staging bot: `/start` → accept terms → start trial → `/settings` → wait for a
signal → `/subscription` → pay (Stars in a test account) → `/account` shows Pro → cancel/resume
auto-renew → `/admin_stats` from the admin account → `/admin_refund <charge_id>`.

## 2. Migrations on production

Migration 007 (multi-exchange) is additive: `signals.exchange` / `commercial_signals.exchange`
(default `bybit`), `instruments` PK `(exchange, symbol)`, `users.enabled_exchanges` default
`{bybit,binance}`, table `app_settings`. Existing Bybit rows stay. Rehearse on a copy:

```bash
DATABASE_URL=postgresql+asyncpg://screener:screener@localhost:55433/screener_rehearsal \
  python scripts/rehearse_migration.py --roundtrip
```

Downgrade to 006 drops only what 007 added, but it **fails once Binance or Bitget
instruments are stored**: the primary key goes back to `symbol` alone and `BTCUSDT` exists for
several exchanges. Delete `instruments` rows with `exchange <> 'bybit'` first; non-Bybit rows in
`signals`/`commercial_signals` then lose their exchange and read as Bybit history.
`AUTO_MIGRATE=false` in production; apply `alembic upgrade head` manually after a backup.

**Geo check:** Binance `fapi` returns HTTP 451 from banned regions. Before enabling
`BINANCE_ENABLED=true` on the server, `curl -sI https://fapi.binance.com/fapi/v1/ping`.

Migration 008 only appends `bitget` to `users.enabled_exchanges` (server default and
existing rows). Downgrade to 007 removes that element and restores the previous default.
A saved admin set in `app_settings` (`admin_exchanges`) is left as stored: if it has no
`bitget`, the admin Bitget engine stays off until the admin turns it on in `/settings`.
A missing key still means every exchange, including Bitget.

Migration 009 adds `users.username` and `users.display_name` (both nullable) so
`/admin_users` can list people who started the bot. Existing rows stay; a name appears
after the next message from that user, or when the admin runs `/admin_users` and Telegram
still knows the chat. Downgrade drops only those two columns.
`AUTO_MIGRATE=false` in production; apply `alembic upgrade head` manually after a backup.
Before `BITGET_ENABLED=true`, confirm instruments from the server:

```bash
curl -s "https://api.bitget.com/api/v3/market/instruments?category=USDT-FUTURES" | head -c 200
```

The payload must contain `"code":"00000"`. Working `.env` can omit `BITGET_*`; the defaults
turn the feed on.

## 2b. Migration 006 on production (historical)

Migration 006 is additive (new tables only). Rehearsed on a copy of the production volume
(revision 003 → 006, 11 674 signals): every pre-existing table had identical row counts and
md5 fingerprints after upgrade, after `downgrade 003` and after re-upgrade.

1. Backup:
   ```bash
   docker compose exec postgres pg_dump -U screener -Fc screener > backup_$(date +%F).dump
   ```
2. Optional rehearsal on a copy (never on production; the script refuses DB names without
   `staging`/`rehearsal`/`test`):
   ```bash
   docker volume create pump_screener_rehearsal_data
   docker run --rm -v pump_screener_postgres_data:/from:ro -v pump_screener_rehearsal_data:/to alpine cp -a /from/. /to/
   docker run -d --name pumpscreener-rehearsal-db -v pump_screener_rehearsal_data:/var/lib/postgresql/data -p 55433:5432 postgres:16-alpine
   docker exec pumpscreener-rehearsal-db psql -U screener -d postgres -c "ALTER DATABASE screener RENAME TO screener_rehearsal"
   DATABASE_URL=postgresql+asyncpg://screener:screener@localhost:55433/screener_rehearsal \
     python scripts/rehearse_migration.py --roundtrip
   docker rm -f pumpscreener-rehearsal-db && docker volume rm pump_screener_rehearsal_data
   ```
3. Set `AUTO_MIGRATE=false` in production `.env` so the app does not migrate on start, then
   migrate manually with the app stopped. With `false` the app exits at startup if the schema
   is not at the code's Alembic head, so a forgotten upgrade fails loudly instead of at runtime:
   ```bash
   docker compose stop app
   docker compose run --rm app alembic upgrade head
   docker compose up -d app
   ```

## 3. Flagged rollout

1. Deploy the new image with `COMMERCIAL_ENABLED=false`. Behaviour is exactly the admin-only bot
   (non-admins get "Доступ запрещён"). Check `/healthz`, admin signals, `/trading`.
2. Fill `TERMS_URL`, `SUPPORT_CONTACT`, `SUBSCRIPTION_PRICE_STARS` (default 1500),
   `TERMS_VERSION`, then set `COMMERCIAL_ENABLED=true` and restart.
3. Watch logs and `/admin_stats` (users, active trials/subscriptions, delivery queue, drops).

Turning the flag off again stops all subscriber traffic; data stays in the commercial tables.

## 4. Payments operations

- **Reconcile** (read-only): compares Telegram Stars transactions with applied payments and
  exits 1 if a paid charge was not applied.
  ```bash
  docker compose exec app python scripts/reconcile_stars.py --days 7
  ```
  A `MISSING` line means a payment Telegram accepted but the DB did not apply (Telegram does not
  redeliver updates). There is no DB row, so `/admin_refund` cannot find it: refund through the
  Bot API `refundStarPayment` (user id + charge id from the reconcile output) or apply manually.
  `/admin_refund` likewise only finds **applied** payments, not anomaly rows.
- **Refund**: `/admin_refund <telegram_payment_charge_id>` (admin only). Access ends immediately,
  auto-renew is cancelled at Telegram.
- Payment anomalies (wrong amount, duplicate subscription, unknown payload) are alerted to the
  admins and logged with internal ids only.

## 5. Rollback

1. Set `COMMERCIAL_ENABLED=false` and restart — enough for any runtime problem.
2. Previous code: compose builds from the working tree (`build: .`), so check out the previous
   release commit and run `docker compose up -d --build app`.
3. Schema: `alembic downgrade 006` drops only the 007 exchange columns/`app_settings`.
   `alembic downgrade 005` drops only the commercial tables (users, configs, trials,
   subscriptions, payments, commercial signals/events). Existing tables are not modified. Commercial
   data is lost, so take a `pg_dump` first.

## 6. Privacy and secrets

- Bybit keys live only in env/secrets; never in PostgreSQL, Telegram, logs or API responses.
- Commercial logs use internal `user_id` (and Telegram id only for anomalies); never full payment
  objects, invoice payloads, message texts or names.
- Invoice payloads are opaque server-generated tokens; user identity always comes from
  `from_user.id` plus a DB check, never from callback data.
