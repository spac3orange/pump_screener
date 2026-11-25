# Bybit Market Screener

Real-time monitor for **crypto USDT Linear Perpetual** contracts on Bybit, Binance (USDⓈ-M)
and Bitget (USDT-M).

Detects Лонг (rise) / Шорт (fall) / Дамп (sharp fall) signals on `lastPrice` from public
ticker streams, stores signals in PostgreSQL, and notifies via Telegram.

> Auto-trading is **optional and Bybit only** (`BYBIT_TRADING_MODE=disabled|demo|mainnet`).
> The screener works without any trading API keys; Binance and Bitget are signals only.

## Stack

- Python 3.12, asyncio, FastAPI
- Bybit V5 public REST + WebSocket (`tickers.{symbol}`)
- PostgreSQL + SQLAlchemy async + Alembic
- aiogram 3 Telegram bot

## Quick start (Docker)

```bash
cp .env.example .env
# Edit TELEGRAM_BOT_TOKEN and ADMIN_ID (your numeric Telegram user id)

docker compose up -d --build
curl http://localhost:8000/healthz
curl http://localhost:8000/readyz
```

### Telegram bot

1. Create a bot via [@BotFather](https://t.me/BotFather) and put the token into `TELEGRAM_BOT_TOKEN`.
2. Get your numeric user id (e.g. via [@userinfobot](https://t.me/userinfobot)) and set `ADMIN_ID=123456789`.
3. Restart: `docker compose up -d --build app`.
4. Open the bot in Telegram and send `/start`.

With `COMMERCIAL_ENABLED=false` only users listed in `ADMIN_ID` can use the bot. With
`COMMERCIAL_ENABLED=true` anyone can start it as a subscriber (trial / Telegram Stars
subscription, see [`docs/COMMERCIAL_RUNBOOK.md`](docs/COMMERCIAL_RUNBOOK.md)); admin commands
stay limited to `ADMIN_ID`. Multiple admins: `ADMIN_ID=111,222`.

## Local development

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux: source .venv/bin/activate

pip install -e ".[dev]"
cp .env.example .env
# Point DATABASE_URL to local Postgres

docker compose up -d postgres
alembic upgrade head
python -m screener.main
```

## Tests

```bash
pytest -q
```

## HTTP endpoints

| Path | Purpose |
|------|---------|
| `GET /healthz` | Process alive |
| `GET /readyz` | Market data ready (WS receiving, instruments > 0) |
| `GET /metrics` | Runtime counters JSON |

## Telegram commands

Admin: `/start` `/status` `/settings` `/profiles` `/signals` `/stats` `/antispam`;
commercial mode adds `/admin_stats` `/admin_users` `/admin_refund <charge_id>` `/account`.

Subscribers (commercial mode): `/start` `/account` `/settings` `/subscription`
`/support` `/paysupport` `/terms`.

Detection rules live in named, versioned **monitoring profiles** stored in PostgreSQL
(`/profiles`). `.env` screener values only seed the system `Default` profile on first start.

## Deploy on Debian VPS

1. Install Docker Engine + Compose plugin.
2. Clone this repo.
3. `cp .env.example .env` and set secrets (never commit `.env`).
4. `docker compose up -d --build`.
5. Check `curl http://127.0.0.1:8000/readyz`.

PostgreSQL is not published on the host port `5432`. The app reaches it as `postgres:5432` inside the Compose network. That avoids a conflict when the VPS already runs its own PostgreSQL.
6. Prefer reverse proxy (Caddy/Nginx) if exposing metrics publicly; otherwise bind to localhost.

Recommended: **2 vCPU / 4 GB RAM / 20–40 GB SSD**.

## Configuration

See [`.env.example`](.env.example).

- **System**: DB URL, exchange endpoints (`BINANCE_ENABLED`, `BITGET_ENABLED`), Telegram token,
  WS shard size.
- **Screener rules**: thresholds, intervals, cooldown — env values seed the `Default` monitoring
  profile once; after that edit them in Telegram `/settings` / `/profiles`.
- **Production**: set `AUTO_MIGRATE=false` and run `alembic upgrade head` yourself; the app then
  refuses to start on a schema that does not match the code.

## Signal price source

All signals use **lastPrice** only (never mark/index).

## License

Private / use at your own risk. Not financial advice.
