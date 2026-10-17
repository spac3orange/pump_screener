# Agent notes — Bybit Market Screener

- Stack: Python 3.12, FastAPI, asyncio, SQLAlchemy async, PostgreSQL, aiogram 3, Bybit V5 public API, Binance USDⓈ-M public API (signals only).
- Signal price source is **lastPrice** only (Bybit `lastPrice`, Binance ticker `c`).
- Monitor crypto USDT Linear Perpetual only (no TradFi, no PreLaunch, no expiry futures).
- Trading is **optional** (`BYBIT_TRADING_MODE=disabled|demo|mainnet`); screener works without private keys.
- Auto-trading is **Bybit only**. Binance signals use `dispatch_monitor_signal` (persist + notify, never `trading.enqueue`); `TradingService.enqueue` also ignores `exchange != bybit`.
- Auto-trading consumes final `MarketSignal` only (never Telegram→order); Telegram `/trading` is ON/OFF + stats.
- Position close notifications: `PositionLifecycleService` (private WS + REST reconcile); close only on
 confirmed `size == 0`, reason only from execution fields (never price), UNKNOWN when unsure;
 exactly-once via `trading_executions.close_notified_at`. Runs even when Trading is OFF.
- Market ticks stay in memory; PostgreSQL stores signals, settings, notifications, system events.
- Prefer extending `SignalEngine` condition handlers over rewriting the pipeline.
- Detection rules live in monitoring profiles (PostgreSQL, immutable versions) via `ProfileService`;
  a profile holds detection settings only — never trading settings or secrets. `.env` only seeds `Default`.
- Never coalesce/drop live lastPrice events for speed; `tests/unit/test_signal_golden.py` guards Bybit behaviour.
- Each exchange has its own `MarketFeed` (registry, WS, processor, `PriceHistory`, engines). Dedup / Signal 24h / correlation keys are `exchange:symbol`.
- User exchange toggles (`users.enabled_exchanges`) are not part of `UserDetectionConfig` / `config_hash`; they only filter groups and fan-out. Default is both Bybit and Binance. The last enabled exchange cannot be turned off (use `/account` to stop all signals).
- Telegram access is gated by `ADMIN_ID` (env). Non-admins are rejected by `AdminAccessMiddleware`
 unless `COMMERCIAL_ENABLED=true`; then `UserContextMiddleware` registers subscribers and admin
 routes stay behind `AdminFilter`.
- Commercial mode (PumpScreener Pro): per-exchange `MultiConfigEngine` → `GroupCorrelator` → shared
  `UserDispatcher` on that exchange's `PriceHistory`, after the admin engine. `GroupSignal` never
  reaches `dispatch_signal`/trading; `application/commercial/*` and `infrastructure/binance/*`
  must not import trading or `bybit_client` (`tests/unit/test_commercial_isolation.py`,
  `tests/unit/test_binance_isolation.py`). Users share one group per effective `config_hash`.
- Access = `users.access_until` (trial end or subscription end, exclusive); checked before every
 send. Trial is one-time and explicit. Stars: access only from `successful_payment`, idempotent by
 `(charge_id, kind)`; never trust user ids from callback data (use `from_user` + DB).
- Subscriber rules: `UserDetectionConfig` (3 rules + min turnover) over a server-side template
 snapshot (`user_profile_templates`), versioned per user. Never edit migrations of shipped schema;
 production sets `AUTO_MIGRATE=false` (see `docs/COMMERCIAL_RUNBOOK.md`).
