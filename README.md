# Bybit Market Screener

Real-time monitor for **crypto USDT Linear Perpetual** contracts on Bybit.

Detects Pump / Dump / Volume Spike signals from public ticker streams, stores history in PostgreSQL, and notifies via Telegram.

> MVP does **not** place orders. No trading API keys required.

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

Only users listed in `ADMIN_ID` can use commands and receive signal alerts. Multiple admins: `ADMIN_ID=111,222`.

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

`/start` `/status` `/settings` `/signals` `/stats` `/help`

Only Telegram user IDs listed in `ADMIN_ID` are accepted.

## Deploy on Debian VPS

1. Install Docker Engine + Compose plugin.
2. Clone this repo.
3. `cp .env.example .env` and set secrets (never commit `.env`).
4. `docker compose up -d --build`.
5. Check `curl http://127.0.0.1:8000/readyz`.
6. Prefer reverse proxy (Caddy/Nginx) if exposing metrics publicly; otherwise bind to localhost.

Recommended: **2 vCPU / 4 GB RAM / 20–40 GB SSD**.

## Configuration

See [`.env.example`](.env.example).

- **System**: DB URL, Bybit endpoints, Telegram token, WS shard size.
- **Screener rules**: thresholds, intervals, cooldown — env defaults, overridable via Telegram `/settings` (stored in `screener_settings`).

## Signal price source

Pump/Dump use **lastPrice** only (never mark/index).

## License

Private / use at your own risk. Not financial advice.
