# Bybit Market Screener — контекст для архитектора

Документ описывает **текущее состояние кода** (октябрь 2026), стек, пайплайн сигналов, настройки, Telegram, деплой и известные ограничения. Цель — передать полный контекст для продолжения работы без перечитывания всего репозитория.

---

## 1. Продукт и границы MVP

**Что это:** круглосуточный скринер резких движений цены по публичному рынку Bybit.

**Что мониторит:** только crypto **USDT Linear Perpetual** в статусе Trading.

**Что исключено:** TradFi (`stock` / `ETF` / `commodity` / `forex`), PreLaunch, срочные фьючерсы, spot.

**Цена для сигналов:** только **`lastPrice`**. Mark/index не используются.

**Чего нет в MVP:**

- торговля / ордера / private Bybit API keys;
- Redis, Kafka, Celery, микросервисы;
- полноценная торговая рекомендация (OI, funding, ликвидации, стакан, объём окна **не входят** в условие сигнала).

**Выход:** Telegram-уведомления + запись сигналов в PostgreSQL + HTTP health/metrics.

---

## 2. Стек

| Слой | Технология |
|------|------------|
| Runtime | Python 3.12, asyncio |
| HTTP | FastAPI + uvicorn |
| Market data | Bybit V5 public REST + WebSocket `tickers.{symbol}` |
| JSON | orjson |
| DB | PostgreSQL 16, SQLAlchemy 2 async (asyncpg), Alembic |
| Bot | aiogram 3, parse mode HTML |
| Config | pydantic / pydantic-settings |
| Deploy | Docker Compose (`app` + `postgres`) |
| Tests | pytest + pytest-asyncio |

Точка входа: `python -m screener.main` / скрипт `screener` → `screener.main:run`.

---

## 3. Структура репозитория

```
src/screener/
  main.py                 # lifespan: migrations → wiring → start workers
  app_state.py            # runtime DI + dispatch_signal + apply_settings
  config.py               # SystemSettings (env) + ScreenerSettings (rules)
  logging.py
  domain/                 # enums, models, pure rules
  application/            # history, processor, engine, correlator, dedup,
                          # registry, metrics, persistence, notifications, supervisor
  workers/market_worker.py
  infrastructure/
    bybit/                # REST, WS, mapping
    db/                   # models, session, repositories
    telegram/             # bot, handlers, formatter, help, settings UI
    http/app.py           # /healthz /readyz /metrics
alembic/versions/
  001_initial_schema.py
  002_drop_signal_type.py # SHORT → DROP
tests/                    # unit + integration + load
scripts/                  # bench_history.py, measure_pipeline.py, smoke_check.py
docs/ARCHITECT_CONTEXT.md # этот файл
```

**Принцип расширения:** новые условия лучше добавлять в `SignalEngine` / domain rules, а не переписывать WS→queue→history пайплайн.

---

## 4. Жизненный цикл процесса

1. Uvicorn поднимает FastAPI с lifespan из `main.build_fastapi()`.
2. `build_runtime()`:
   - Alembic `upgrade head` (в thread);
   - engine/session/repo;
   - `load_effective_settings(repo, env_defaults)` — env как дефолт, overlay из `screener_settings` id=1;
   - создаёт history, processor, engine, correlator, WS, registry, persistence, notifications;
   - Telegram, если заданы `TELEGRAM_BOT_TOKEN` и `ADMIN_ID`.
3. Start order: persistence → notifications → `MarketWorker.start()` → TG polling.
4. Stop order: worker → notifications → TG → persistence drain → REST close → engine dispose.

`MarketWorker.start()`:

1. Вешает WS callbacks.
2. `registry.start()` — REST universe + периодический refresh.
3. Стартует process / readiness / loop-monitor tasks.
4. `ws.start(symbols)`.
5. Фоновый REST kline warmup (только в history, не в TickerState).

**Важно (баг, уже исправлен):** при первом refresh registry вызывает `on_change` → `ws.update_symbols()` **до** `ws.start()`. Раньше шарды создавались без задач. Сейчас `start()` поднимает задачи у уже существующих шардов. Регрессия: `tests/integration/test_ws_dynamic_subscribe.py::test_start_launches_shards_planned_before_start`.

**Логи:** в `alembic/env.py` стоит `disable_existing_loggers=False`, иначе миграция глушила логи приложения после старта.

---

## 5. Пайплайн рыночных данных (горячий путь)

```
Bybit WS tickers.*
    → WebSocketManager (shard reader, await on_message)
    → MarketDataProcessor.apply_message  # merge snapshot/delta → PriceEvent|None
    → asyncio.Queue[PriceEvent]          # lossless; full queue = TCP backpressure
    → MarketWorker.process_event
         → PriceHistoryManager.record
         → SignalEngine.evaluate (только LIVE lastPrice change)
              → RuleMatch[] → SignalCorrelator.add
                   → MarketSignal → AppState.dispatch_signal
                        ├─ PersistenceService (ordered DB write)
                        └─ NotificationService (Telegram, independent)
```

### 5.1 WebSocket

- URL: `wss://stream.bybit.com/v5/public/linear`.
- Топики: `tickers.{symbol}`.
- Шардинг: ~100 символов на соединение (`WS_SYMBOLS_PER_CONNECTION`).
- Динамический subscribe/unsubscribe с `req_id` и ack-tracking.
- Reconnect только при ошибке / stale / ack timeout (не при обычном изменении universe).
- Ping: `{"op":"ping"}` каждые `WS_PING_INTERVAL_SEC` (10).
- Stale: нет любого кадра (data/pong) дольше `STALE_CONNECTION_SEC` (30) → reconnect.
- Ack timeout: `WS_ACK_TIMEOUT_SEC` (10).
- Freshness соединения: `time.monotonic()`.
- Время для истории/окон: Bybit `ts` (ms).
- Сырые кадры **не дропаются**: `await on_message`; при полной очереди price events reader ждёт.

### 5.2 Snapshot / delta → PriceEvent

`TickerState.apply(...)`:

- snapshot → seed состояния + `PriceEventKind.SNAPSHOT_SEED` (в историю пишется, **engine не считает**);
- delta с изменением `lastPrice` → `LIVE`;
- delta без `lastPrice` (bid/OI/funding enrichment) → метрика, событие не эмитится;
- delta до snapshot → discard + counter.

OI/funding хранятся в памяти тикера, **не участвуют** в правилах.

### 5.3 История цен

`PriceHistoryManager` — per-symbol ring buffer 1 Гц:

- capacity ≈ 3840 сек (3600 + tolerance + margin);
- storage: `array('q')` seconds, `array('d')` prices, `bytearray` source;
- lookup O(1) по секунде окна + scan в пределах tolerance `max(2s, 5% window)`;
- carry-forward цены внутри непрерывного соединения;
- disconnect → gap; окно инвалидируется, только если gap пересекает reference и длиннее tolerance;
- качество: `KLINE` / `MIXED` / `LIVE`;
- REST 1m klines: warmup при старте и gap backfill после reconnect (≥ `GAP_BACKFILL_MIN_SEC`); kline **не перезаписывает** уже живые секунды и **не трогает** TickerState/cs.

### 5.4 SignalEngine

Три правила (включаются флагами):

| UI label | SignalType | Условие | Env defaults |
|----------|------------|---------|--------------|
| Лонг | PUMP | `change >= +threshold` | +40% / 12 мин (720s) |
| Шорт | DROP | `change <= -threshold` | −10% / 30 мин (1800s) |
| Дамп | DUMP | `change <= -threshold` | −40% / 15 мин (900s) |

Формула:

```
change% = (lastPrice_now − price_at_window_start) / price_at_window_start × 100
```

Дополнительно на каждое правило:

- `turnover_24h >= min_turnover_24h_usdt` (дефолт $100k);
- символ не в `exclude_symbols`;
- `allow_mixed_history` (дефолт true) — иначе MIXED/KLINE reference отбрасывается;
- один lookup на **уникальный** interval_sec за событие;
- anti-spam через `SignalDeduplicator` **per (symbol, signal_type, interval)**.

**Не считать сигнал торговой стороной:** PUMP ≠ «открыть Long», DROP/DUMP ≠ «открыть Short». Naming: market events.

### 5.5 Dedup (anti-spam)

Параметры из `ScreenerSettings` (не хардкод в help):

- `cooldown_sec` (300) — минимум между повторными уведомлениями;
- `escalate_step_pct` (2.0) — повтор только если |change| ≥ peak + step;
- `reset_ratio` (0.5) + `reset_hold_sec` (60) — сброс состояния после ослабления движения.

### 5.6 Correlator

`SignalCorrelator` группирует RuleMatch по `(symbol, direction)` в окне `correlation_window_ms` (дефолт 400):

- DROP+DUMP одного движения → один `MarketSignal`, primary = max(SEVERITY, |change|); DUMP severity выше;
- PUMP и падение **не** мержатся;
- независимые по времени срабатывания — отдельные сообщения.

### 5.7 Persistence vs Telegram

Независимы:

- `PersistenceService` — bounded deque, ordered retry, slow DB не блокирует алерты;
- `NotificationService` — очередь Telegram; `Signal 24h` = max(memory, 1+DB count) с timeout (`signal_count_db_timeout_sec`, 2s) и fallback на память;
- Telegram `RetryAfter` ретраится в `bot.send_signal`.

---

## 6. Формат Telegram-алерта

Рендер: `infrastructure/telegram/formatter.py` → `format_signal_message`.

Пример (несколько правил):

```
ByBit – 15M – ABC
🔻 Dump: -42.00% (1-0.58)
🔴 Drop 30M: -47.00% (1.09434-0.58)
Signal 24h: 1
```

- первая цена в скобках — ориентир начала окна;
- вторая — `lastPrice` на момент детекта;
- `Signal 24h` — число уведомлений по символу за 24ч, включая текущее (merged alert = 1);
- эмодзи в алерте: Pump/Drop = 🔴, Dump = 🔻 (не совпадает 1:1 с `/settings` 🟢/🔴/🔻).

Ссылка на торговлю: `https://www.bybit.com/trade/usdt/{symbol}` (есть helper, в текущем тексте алерта может не использоваться — смотреть formatter).

---

## 7. Настройки

### 7.1 Два слоя

1. **SystemSettings** — только env (DB, Bybit URLs, WS, TG token, ADMIN_ID, concurrency…).
2. **ScreenerSettings** — правила скринера:
   - дефолты из env при старте;
   - runtime overlay в PostgreSQL `screener_settings` (row `id=1`, колонка `config` JSONB);
   - изменение через Telegram `/settings` → `AppState.apply_settings` → engine + correlator + registry exclude + DB save.

Источник истины для правил в рантайме: **`state.signal_engine.settings`**.

### 7.2 Telegram UI настроек

- Кнопки ON/OFF: Лонг / Шорт / Дамп.
- Шесть кнопок period/pct → ответ числом в чат.
- Интервалы: целые минуты 1…60 (`*_interval_sec` кратно 60, ≤ 3600).

### 7.3 `/help`

Динамическая справка из `help_text.build_help_messages(settings)` — 3 HTML-сообщения с текущими порогами/интервалами/оборотом/cooldown. Не хардкодит дефолты. Тесты: `tests/unit/test_help_text.py`.

### 7.4 Доступ

`AdminAccessMiddleware` — только `ADMIN_ID` (comma-separated). Остальные отклоняются.

---

## 8. База данных

| Таблица | Назначение |
|--------|------------|
| `instruments` | снимок universe |
| `signals` | сигналы + JSONB `payload` (в т.ч. matches) |
| `screener_settings` | JSON config id=1 |
| `notification_history` | delivery status per chat |
| `system_events` | operational events |

Миграция `002`: значения `signal_type='SHORT'` → `'DROP'`.

Тики **не** пишутся в БД — только память.

---

## 9. HTTP

| Path | Смысл |
|------|--------|
| `GET /healthz` | процесс жив |
| `GET /readyz` | ready = instruments>0 ∧ ws_connections>0 ∧ last frame fresh; иначе 503 |
| `GET /metrics` | JSON counters / rates / latency percentiles |

Ready loop обновляет `metrics.ready` раз в секунду.

---

## 10. Метрики (ключевые)

Обязательные инварианты пайплайна:

- `raw_protocol_drops_total = 0` (по конструкции: нет drop-oldest на price path);
- `lost_price_events = price_events_in − processed − queue_size` → должен быть 0 в норме.

Также: frames, live lastPrice/s, engine evals/s, history lookups, gaps/backfills, reconnects, subscribe acks/failures, p50/p95/p99 pipeline latency (`perf_counter`), event loop lag, task restarts.

Замеры (локально, ~521 символ, 60s, без DB):

| | Before (legacy) | After (текущий) |
|--|-----------------|-----------------|
| CPU | ~12% | ~5% |
| RSS | ~280 MB | ~90 MB |
| Engine evals | ~все msgs (~600/s) | только live lastPrice (~50/s) |
| Lookup 30m | ~43 µs | ~1.5–2 µs |

Скрипты: `scripts/bench_history.py`, `scripts/measure_pipeline.py`.

---

## 11. Telegram-команды

| Команда | Содержание |
|---------|------------|
| `/start` | краткое описание |
| `/help` | 3 сообщения: правила, чтение алерта, ограничения |
| `/status` | ready, instruments, WS, msg/s, live price/s, drops, lost, reconnects, gaps, p95 |
| `/settings` | текущие правила + клавиатура |
| `/signals` | последние 10 из БД |
| `/stats` | counts из БД + runtime |

---

## 12. Деплой (VPS Docker)

```bash
cp .env.example .env   # TELEGRAM_BOT_TOKEN, ADMIN_ID, при необходимости новые WS_* ключи
docker compose up -d --build
curl http://127.0.0.1:8000/readyz
docker logs pumpscreener-app-1
```

- Postgres **не** публикуется на host:5432 (избегает конфликта с системным PG).
- App → `postgres:5432` внутри Compose network.
- Рекомендация железа: 2 vCPU / 4 GB / 20–40 GB SSD.
- После деплоя в логах ожидаются: universe, `WS universe: N symbols across M shards`, kline warmup, `Screener started`.
- `/status`: `Ready: True`, `WS connections` ≈ ceil(instruments/100), Msg/s ~ сотни.

Новые env-ключи (после hardening):  
`WS_PING_INTERVAL_SEC`, `STALE_CONNECTION_SEC`, `WS_ACK_TIMEOUT_SEC`, `KLINE_WARMUP_CONCURRENCY`, `GAP_BACKFILL_MIN_SEC`, `ALLOW_MIXED_HISTORY`, `SIGNAL_CORRELATION_MS`.  
Удалённые (volume spike и т.п.) игнорируются (`extra="ignore"`).

---

## 13. Тесты — что покрыто

Критичные регрессии:

- spike `100 → 141 → 120` детектится как PUMP; lost/raw drops = 0;
- короткий disconnect после длинной истории не инвалидирует 30m;
- длинный gap инвалидирует только затронутые окна; backfill восстанавливает;
- DROP+DUMP корреляция в одно сообщение;
- snapshot/enrichment не вызывают evaluate;
- notification при «зависшей» DB всё равно уходит;
- dynamic WS subscribe + start-after-update_symbols;
- `/help` динамические settings / disabled rule / HTML escape / длина < 4096.

Запуск: `pytest -q` (pythonpath `src`).

---

## 14. Известные ограничения и честные продуктые формулировки

1. Сигнал = факт движения `lastPrice` + фильтр оборота 24ч, не рекомендация.
2. Нет объёма окна, OI, funding, ликвидаций, order book imbalance в условии.
3. После рестарта/долгого обрыва reference price может быть MIXED (REST kline) — приблизительная.
4. Dedup/корреляция — эвристики; независимые по времени DROP затем DUMP могут прийти двумя сообщениями.
5. MVP single-process; горизонтального шардинга приложения нет (только WS shard connections).
6. В алерте эмодзи Pump/Drop одинаковые (🔴) — исторический Bybit-like формат.

---

## 15. Карта решений «куда лезть»

| Задача | Где |
|--------|-----|
| Новое ценовое правило | `domain/rules.py` + `signal_engine.py` + settings/Telegram UI |
| Изменить формат алерта | `telegram/formatter.py` (+ help_text sample) |
| Изменить anti-spam | `dedup.py` + ScreenerSettings fields |
| Universe filter | `bybit/mapping.py` |
| История / gaps | `price_history.py` + worker warmup/backfill |
| WS поведение | `bybit/ws.py` |
| Независимая доставка | `persistence_service.py` / `notification_service.py` |
| Текст `/help` | `telegram/help_text.py` |
| Миграции схемы | `alembic/versions/` |

---

## 16. Быстрый glossary

- **PUMP / DROP / DUMP** — типы рыночных событий (не торговые стороны).
- **Лонг / Шорт / Дамп** — подписи кнопок Telegram для тех же правил.
- **PriceEvent** — единица работы очереди (snapshot_seed | live).
- **RuleMatch** — сработавшее правило с % и ценами.
- **MarketSignal** — user-facing событие (1+ matches после корреляции).
- **raw protocol drops** — потери кадров на WS path (цель: 0).
- **lost price events** — in − processed − queue (цель: 0).

---

*Сгенерировано по состоянию репозитория `pump_screener`. При расхождении с кодом приоритет у кода.*
