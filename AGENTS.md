# Agent notes — Bybit Market Screener

- Stack: Python 3.12, FastAPI, asyncio, SQLAlchemy async, PostgreSQL, aiogram 3, Bybit V5 public API.
- Signal price source is **lastPrice** only.
- Monitor crypto USDT Linear Perpetual only (no TradFi, no PreLaunch, no expiry futures).
- MVP has **no trading** / no private Bybit keys.
- Market ticks stay in memory; PostgreSQL stores signals, settings, notifications, system events.
- Prefer extending `SignalEngine` condition handlers over rewriting the pipeline.
- Telegram access is gated by `ADMIN_ID` (env). Non-admins are rejected by `AdminAccessMiddleware`.
