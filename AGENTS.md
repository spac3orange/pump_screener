# Agent notes — Bybit Market Screener

- Stack: Python 3.12, FastAPI, asyncio, SQLAlchemy async, PostgreSQL, aiogram 3, Bybit V5 public API.
- Signal price source is **lastPrice** only.
- Monitor crypto USDT Linear Perpetual only (no TradFi, no PreLaunch, no expiry futures).
- Trading is **optional** (`BYBIT_TRADING_MODE=disabled|demo|mainnet`); screener works without private keys.
- Auto-trading consumes final `MarketSignal` only (never Telegram→order); Telegram `/trading` is ON/OFF + stats.
- Position close notifications: `PositionLifecycleService` (private WS + REST reconcile); close only on
 confirmed `size == 0`, reason only from execution fields (never price), UNKNOWN when unsure;
 exactly-once via `trading_executions.close_notified_at`. Runs even when Trading is OFF.
- Market ticks stay in memory; PostgreSQL stores signals, settings, notifications, system events.
- Prefer extending `SignalEngine` condition handlers over rewriting the pipeline.
- Detection rules live in monitoring profiles (PostgreSQL, immutable versions) via `ProfileService`;
  a profile holds detection settings only — never trading settings or secrets. `.env` only seeds `Default`.
- Never coalesce/drop live lastPrice events for speed; `tests/unit/test_signal_golden.py` guards behavior.
- Telegram access is gated by `ADMIN_ID` (env). Non-admins are rejected by `AdminAccessMiddleware`
 unless `COMMERCIAL_ENABLED=true`; then `UserContextMiddleware` registers subscribers and admin
 routes stay behind `AdminFilter`.
- Commercial mode (PumpScreener Pro): `MultiConfigEngine` → `GroupCorrelator` → `UserDispatcher`
 on the shared `PriceHistory`, after the admin engine. `GroupSignal` never reaches
 `dispatch_signal`/trading; `application/commercial/*` must not import trading or `bybit_client`
 (`tests/unit/test_commercial_isolation.py`). Users share one group per effective `config_hash`.
- Access = `users.access_until` (trial end or subscription end, exclusive); checked before every
 send. Trial is one-time and explicit. Stars: access only from `successful_payment`, idempotent by
 `(charge_id, kind)`; never trust user ids from callback data (use `from_user` + DB).
- Subscriber rules: `UserDetectionConfig` (3 rules + min turnover) over a server-side template
 snapshot (`user_profile_templates`), versioned per user. Never edit migrations of shipped schema;
 production sets `AUTO_MIGRATE=false` (see `docs/COMMERCIAL_RUNBOOK.md`).
