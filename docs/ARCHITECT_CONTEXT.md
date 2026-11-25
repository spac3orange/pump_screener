# Bybit Market Screener — контекст для архитектора

Документ описывает **текущее состояние кода** (октябрь 2026), стек, пайплайн сигналов, настройки, Telegram, деплой и известные ограничения. Цель — передать полный контекст для продолжения работы без перечитывания всего репозитория.

---

## 1. Продукт и границы MVP

**Что это:** круглосуточный скринер резких движений цены по публичным рынкам Bybit, Binance и Bitget.

**Что мониторит:** только crypto **USDT Linear Perpetual** в статусе Trading (Binance: `PERPETUAL` + `underlyingType=COIN`; Bitget: `type=perpetual`, `symbolType=crypto`, `isRwa != YES`).

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
| Market data | Bybit V5 public REST + WebSocket `tickers.{symbol}`; Binance USDⓈ-M `!ticker@arr`; Bitget UTA v3 ticker per symbol (monitoring only) |
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
    telegram/             # bot, handlers, formatter, settings UI
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
   - `ProfileService.bootstrap()` — активный мониторинг-профиль из PostgreSQL; при первом запуске
     прежние настройки (`env` + legacy `screener_settings` id=1) один раз переносятся в системный `Default`;
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
                        ├─ NotificationService (Telegram, if exchange is on for the admin)
                        └─ TradingService.enqueue (Bybit only)
```

Binance (`BinanceTickerStream` → `BinanceTickerProcessor`) and Bitget (`BitgetTickerStream` → `BitgetTickerProcessor`) each have their own `MarketFeed`, `PriceHistory` and engines. Their `MarketSignal` goes to `dispatch_monitor_signal` (persist + notify, never trading). Bitget has no all-market ticker: about 40 symbols per connection, text `ping` / `pong`, envelope `ts`. `/readyz` stays Bybit-only because trading depends on it; other feeds degrade via nested `feeds.*` metrics and a `system_events` row.

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

Пример (в сообщении только основное правило):

```
ByBit – 15M – ABC
🔻 Дамп: -42.00% (1-0.58)
🔉 Signal 24h: 1
```

- первая цена в скобках — ориентир начала окна;
- вторая — `lastPrice` на момент детекта;
- `Signal 24h` — число уведомлений по символу за 24ч, включая текущее (merged alert = 1);
- подписи и эмодзи совпадают с `/settings`: PUMP = 🟢 Лонг, DROP = 🔴 Шорт, DUMP = 🔻 Дамп. Это название правила, а не действие автоторговли (`PUMP_ACTION`, `DROP_ACTION`, `DUMP_ACTION`).

Ссылка на торговлю: `https://www.bybit.com/trade/usdt/{symbol}` (есть helper, в текущем тексте алерта может не использоваться — смотреть formatter).

---

## 7. Настройки

### 7.1 Два слоя

1. **SystemSettings** — только env (DB, Bybit/Binance/Bitget URLs, WS, TG token, ADMIN_ID, concurrency…). `BINANCE_ENABLED=false` / `BITGET_ENABLED=false` выключает весь feed этой биржи.
2. **ScreenerSettings** — правила скринера, хранятся в **мониторинг-профилях** (PostgreSQL — источник истины):
   - профиль = полный снимок `ScreenerSettings.canonical_config()` (не overlay), версии неизменяемые;
   - `config_schema_version` + `config_hash` (sha256 канонического JSON; `rule_version` = первые 12 символов);
   - env-значения `SCREENER_*`/правил используются только для первичного seed `Default`;
   - в профиле **только detection-настройки**: никаких trading-параметров, токенов, ключей, DB URL.

Источник истины для правил в рантайме: **`state.signal_engine.settings`** (иммутабельный
`_EngineConfig`, подменяется одной ссылкой). Активный профиль: `state.profiles.active`.

### 7.1.1 ProfileService (`application/profiles/service.py`)

- Любая мутация: валидация → запись в БД → только потом синхронный `AppState.apply_profile`
  (engine + correlator window + registry exclude). Ошибка валидации/БД — старый профиль остаётся.
- `activate`: сбрасывает config-зависимый dedup; PriceHistory, WS, registry **не** трогаются;
  пишет `system_events.monitoring_profile_changed`.
- `/settings` → `update_active_settings` → новая версия активного профиля (тот же hash — no-op).
- Нельзя удалить активный и системный `Default`; удаление мягкое (`deleted_at`), версии остаются
  для provenance сигналов; имя после удаления снова свободно. Имена уникальны без учёта регистра.
- Каждый сигнал получает `SignalContext` в момент evaluate: `monitoring_profile_id`,
  `monitoring_profile_version_id`, `rule_version`. Correlator не смешивает matches разных контекстов
  в одном сигнале → событие видит старый или новый конфиг, никогда гибрид.

### 7.2 Telegram UI настроек

- Кнопки ON/OFF: Лонг / Шорт / Дамп.
- Шесть кнопок period/pct → ответ числом в чат.
- Интервалы: целые минуты 1…60 (`*_interval_sec` кратно 60, ≤ 3600).
- Сверху «Активный профиль: X»; изменения сохраняются новой версией активного профиля.
- `/profiles`: список → карточка профиля → Активировать / Копия / Переименовать / Удалить,
  «Создать из текущих», «Настройки». Если автоторговля ON, активация требует подтверждения
  (торговля при этом не выключается).

### 7.3 Доступ

`AdminAccessMiddleware` — только `ADMIN_ID` (comma-separated). Остальные отклоняются.
При `COMMERCIAL_ENABLED=true` вместо него `UserContextMiddleware` (см. §7.4): admin-роутер
закрыт `AdminFilter`, остальные пользователи попадают только в роутеры подписчика и оплаты.

### 7.4 Коммерческий режим (PumpScreener Pro)

Флаг `COMMERCIAL_ENABLED` (по умолчанию `false` — бот ровно как раньше).

```
PriceEvent (LIVE) ── SignalEngine ── SignalCorrelator ── dispatch_signal ── persistence / admin TG / trading
        └────────── MultiConfigEngine ── GroupCorrelator ── UserDispatcher ── Telegram подписчикам
                         ↑ группы по config_hash         ↑ ActiveUserRegistry (access_until, chat_id)
```

- Один рыночный пайплайн и одна `PriceHistory`; коммерческий движок вызывается после admin-движка
  в отдельном `try` и не влияет на него. `GroupSignal` — отдельный тип, в trading не попадает
  (AST-тест + runtime-тест `tests/unit/test_commercial_isolation.py`).
- Пользователи с одинаковыми эффективными настройками = одна группа (`ScreenerSettings.config_hash()`
  от template + пользовательских полей). Индекс `(type, interval) → отсортированные пороги`, bisect;
  у каждой группы свой `SignalDeduplicator` (семантика = `SignalEngine`, тест эквивалентности).
- Пользователь меняет только 3 правила (вкл/%, период 1–60 мин) и мин. оборот 24ч
  (`UserDetectionConfig`); cooldown/escalate/reset/корреляция — из template `user_profile_templates`,
  засеянного один раз из активного admin-профиля. Версии конфигов неизменяемы.
- Доступ: `users.access_until` = max(конец trial, конец подписки [+grace при автопродлении]),
  граница исключающая. Проверяется диспетчером перед каждой отправкой, поэтому доставка
  останавливается вовремя даже без `ExpirationWorker`. Admin — bypass по роли; коммерческая копия
  сигналов для admin выключена по умолчанию (`/account`).
- Trial: только по кнопке, один раз (атомарный `UPDATE … WHERE trial_started_at IS NULL`), не после
  оплаты, требует принятия `/terms` (`TERMS_VERSION`).
- Stars: `createInvoiceLink(XTR, subscription_period=30д)`, payload `ps1_<random>` создаётся сервером.
  `pre_checkout` (≤3с, без выдачи доступа) проверяет владельца/сумму/срок. Доступ только из
  `successful_payment` в одной транзакции, идемпотентно по `(telegram_payment_charge_id, kind)`;
  продление = новый charge с тем же payload. Аномалии (сумма, чужой счёт, вторая подписка…)
  записываются и алертятся админам; вторая подписка отменяется в Telegram. Отмена/возобновление
  автопродления — `editUserStarSubscription`, затем локально. Возврат — `/admin_refund` или
  `refunded_payment` → доступ отозван. Сверка read-only: `scripts/reconcile_stars.py`.
- Доставка: bounded fan-out, per-user очередь (drop-oldest), глобальный темп `USER_SEND_RATE_PER_SEC`,
  ≥`USER_CHAT_MIN_INTERVAL_SEC` на чат, один сигнал — одно сообщение, старше
  `USER_SIGNAL_MAX_AGE_SEC` не отправляются, 429 → общая пауза, Forbidden → `bot_blocked` и удаление
  из реестра (снимается при следующем сообщении пользователя). Итог по каждому пользователю —
  `user_signal_events` через упорядоченный ретрай-писатель (сигнал пишется раньше событий).
- `ExpirationWorker` (каждые `EXPIRY_CHECK_SEC`): `expire_subscriptions`, затем claim
  `expiry_notified_until = access_until` (одно уведомление на период доступа, и после рестарта),
  раз в час чистка сигналов старше `USER_SIGNAL_RETENTION_DAYS`.
- Нагрузка (`scripts/benchmark_commercial.py`, 500 символов, 1000 пользователей / 253 группы):
  live-событие p50 18.6→38.1 µs, p99 39→117 µs; admin-сигналы идентичны.

---

## 8. База данных

| Таблица | Назначение |
|--------|------------|
| `instruments` | снимок universe |
| `signals` | сигналы + JSONB `payload` (в т.ч. matches, `monitoring_profile`) + `monitoring_profile_id`, `monitoring_profile_version_id` |
| `monitoring_profiles` | имя, описание, `is_system`, `current_version_id`, `deleted_at` |
| `monitoring_profile_versions` | неизменяемые `config` JSONB + `config_schema_version` + `config_hash` |
| `monitoring_runtime_state` | одна строка (`id=1`): `active_profile_id` |
| `screener_settings` | **deprecated**: читается один раз для seed `Default` |
| `notification_history` | delivery status per chat |
| `system_events` | operational events |

Миграция `002`: значения `signal_type='SHORT'` → `'DROP'`.
Миграция `004`: таблицы профилей + колонки provenance в `signals` (nullable; старые сигналы без профиля).
Seed `Default` делает приложение (`ProfileService.bootstrap`), детерминированно из прежних настроек.
Миграция `005`: lifecycle-колонки в `trading_executions` (все nullable, старые строки не ломаются;
`opened_at` для уже исполненных = `created_at`).

Миграция `006` (только добавляет таблицы): `users`, `user_profile_templates`,
`user_monitoring_profiles` / `user_monitoring_profile_versions`, `payment_invoices`, `subscriptions`
(частичный unique «одна живая подписка на пользователя»), `payments` (unique `charge_id+kind`),
`commercial_signals`, `user_signal_events` (unique `user_id+signal_id`). Downgrade удаляет только их.

Тики **не** пишутся в БД — только память.

### 8.1 Жизненный цикл позиции (закрытие сделки → Telegram)

`OPEN → monitor → size == 0 → classify → persist → ровно одно уведомление`.

```
Bybit private WS (order / execution / position)   bybit_client/private_ws.py
        ↓  PrivateMessageRouter                    infrastructure/bybit/lifecycle_adapter.py
        ├─ fill hints → BybitGateway.wait_for_fill (вход не изменён)
        ↓
PositionLifecycleService                           application/trading/lifecycle.py
        ↓ classify_close                           application/trading/close_classifier.py
TradingRepository (LifecycleStore)                 infrastructure/db/trading_repo.py
        ↓ TradeClosedEvent
TelegramBotService.notify_trade_closed             telegram/trade_lifecycle_format.py
```

- Один private WS на процесс (auth HMAC, `ping` 20с, stale 60с → reconnect, backoff+jitter,
  resubscribe). pybit WS в gateway выключен (`start_pybit_ws=False`), его fill-hints кормит router.
- Закрыта = подтверждённый `size == 0` (WS, затем повторно REST `position/list`); REST `size > 0`
  отменяет финализацию. Partial reduction → только `remaining_qty`.
- Причина — только из полей execution закрывающего ордера (последнего по времени):
  `stopOrderType`/`createType` TakeProfit/StopLoss (или сохранённый `tp_order_id`/`sl_order_id`),
  `CreateByClosing` → MANUAL (кнопка Close в Bybit), `CreateByUser` → EXTERNAL_ORDER,
  `BustTrade`/`CreateByLiq` → LIQUIDATION, `AdlTrade` → ADL (расширение). Противоречие/нет данных →
  UNKNOWN. Cancelled/Deactivated TP/SL не является доказательством. Цена не используется.
- Числа: realized = Σ`execPnl` (WS; REST execution/list его не отдаёт), fees = execFee входа +
  закрытия, funding = Σ `execType=Funding` за жизнь сделки, net = realized − fees − funding.
  Если чего-то нет — поле `NULL` и строка в сообщении не выводится. `closed-pnl` Bybit — сверка /
  fallback (`Net PnL (Bybit)`), только если есть записи по **всем** закрывающим ордерам.
- Порядок событий: окно корреляции (`LIFECYCLE_CORRELATION_WINDOW_SEC`) после `size=0`, сразу —
  если закрывающих executions хватает на весь qty; ограниченный повтор REST при отставании истории.
- Exactly-once: `finalize_close` = `UPDATE … WHERE status <> 'CLOSED'`; уведомление —
  claim `close_notified_at IS NULL`; при ошибке отправки claim снимается и reconcile повторяет.
- Restart: OPEN/CLOSING/FILLED строки → REST позиции → закрытые финализируются (`recovered`),
  уведомление один раз, старше 6ч — только запись. Reconcile каждые `LIFECYCLE_RECONCILE_INTERVAL_SEC`
  и после каждого reconnect WS. WS down > 120с → critical alert в Telegram.
- Работает при Trading OFF (OFF запрещает только новые входы). Нужны ключи и
  `orders_environment_ok()`; `TRADING_NOTIFY_CLOSES=false` — запись без сообщения.
- Ограничения: MANUAL распознаётся только для кнопки Close (`CreateByClosing`); рыночный
  reduce-only ордер из UI/API → EXTERNAL_ORDER. Balance after close — equity сразу после закрытия
  (при восстановлении после рестарта не показывается). Funding за последние 7 дней окна REST.

Smoke (только DEMO): `scripts/demo_lifecycle_smoke.py --runs tp sl external`.

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

Скрипты: `scripts/bench_history.py`, `scripts/measure_pipeline.py`,
`scripts/benchmark_performance.py`, `scripts/soak_synthetic.py` (подробно — `docs/PERFORMANCE.md`).

---

## 11. Telegram-команды

| Команда | Содержание |
|---------|------------|
| `/start` | краткое описание |
| `/status` | Monitoring profile, ready, instruments, WS, msg/s, live price/s, drops, lost, reconnects, gaps, p95 |
| `/settings` | правила активного профиля + клавиатура |
| `/profiles` | мониторинг-профили: активация, создание, копия, переименование, удаление |
| `/signals` | последние 10 из БД (с профилем и версией) |
| `/stats` | counts из БД + runtime |
| `/admin_stats` | (commercial) пользователи, trial, подписки, конверсия, выручка 30д, аномалии, доставка |
| `/admin_refund <charge_id>` | (commercial) возврат Stars + отзыв доступа |

Подписчики (только при `COMMERCIAL_ENABLED=true`): `/start`, `/account`, `/settings` (свои правила),
`/subscription` (оплата, отмена/возобновление автопродления), `/terms`, `/support`, `/paysupport`.
Неизвестные и admin-команды → «Неизвестная команда».

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
`WS_PING_INTERVAL_SEC`, `STALE_CONNECTION_SEC`, `WS_ACK_TIMEOUT_SEC`, `KLINE_WARMUP_CONCURRENCY`, `GAP_BACKFILL_MIN_SEC`, `ALLOW_MIXED_HISTORY`, `SIGNAL_CORRELATION_MS`, `WS_COMPRESSION` (`deflate`|`none`), `WS_MAX_QUEUE`.  
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
- golden-фикстура сигналов (`tests/unit/test_signal_golden.py`, в т.ч. короткий спайк с пересечением порога внутри секунды);
- профили: CRUD, миграция before == after, атомарная активация, no-hybrid при конкурентной смене,
  provenance сигнала (`tests/unit/test_profiles.py`);
- реальный PostgreSQL (opt-in): `TEST_DATABASE_URL=...screener_test pytest tests/integration/test_profiles_db.py`;
- закрытие позиций: TP/SL/cancelled-legs/partial/manual/external/unknown/liquidation, дубликаты WS,
  порядок событий, restart recovery, Trading OFF, повтор доставки (`tests/unit/test_trade_lifecycle.py`,
  `test_close_classifier.py`, `test_trade_lifecycle_format.py`, `test_private_stream.py`,
  `tests/integration/test_trade_lifecycle_db.py`).

- коммерческий режим: эквивалентность `MultiConfigEngine` N независимым `SignalEngine`
  (`test_multi_engine.py`), реестр, доступ/граница, диспетчер (rate limit, 429, Forbidden, max-age,
  склейка), state machine оплат, изоляция от trading, admin-only при выключенном флаге;
  на PostgreSQL: миграция 006 up/down, атомарный trial, идемпотентная оплата при гонке, продление,
  отмена/возобновление, возврат, одно уведомление об окончании (`test_commercial_db.py`) и
  end-to-end бот с mock Telegram API (`test_commercial_bot.py`).

Запуск: `pytest -q` (pythonpath `src`). Перф: `docs/PERFORMANCE.md`.

---

## 14. Известные ограничения и честные продуктые формулировки

1. Сигнал = факт движения `lastPrice` + фильтр оборота 24ч, не рекомендация.
2. Нет объёма окна, OI, funding, ликвидаций, order book imbalance в условии.
3. После рестарта/долгого обрыва reference price может быть MIXED (REST kline) — приблизительная.
4. Dedup/корреляция — эвристики; независимые по времени DROP затем DUMP могут прийти двумя сообщениями.
5. MVP single-process; горизонтального шардинга приложения нет (только WS shard connections).
6. Подпись «Лонг» / «Шорт» в алерте — имя правила из `/settings`; автоторговля выбирает сторону сделки отдельно по `*_ACTION`.

---

## 15. Карта решений «куда лезть»

| Задача | Где |
|--------|-----|
| Новое ценовое правило | `domain/rules.py` + `signal_engine.py` + settings/Telegram UI |
| Изменить формат алерта | `telegram/formatter.py` |
| Изменить anti-spam | `dedup.py` + ScreenerSettings fields |
| Universe filter | `bybit/mapping.py` |
| История / gaps | `price_history.py` + worker warmup/backfill |
| WS поведение | `bybit/ws.py` |
| Независимая доставка | `persistence_service.py` / `notification_service.py` |
| Миграции схемы | `alembic/versions/` |
| Причина закрытия сделки | `application/trading/close_classifier.py` |
| Формат сообщения о закрытии | `telegram/trade_lifecycle_format.py` |
| Правила/группы подписчиков | `application/commercial/multi_engine.py`, `user_service.py` |
| Доступ, trial, подписка | `application/commercial/access.py`, `trial_service.py`, `subscription_service.py` |
| Оплата Stars | `application/commercial/payment_service.py`, `telegram/payment_handlers.py` |
| Доставка подписчикам | `application/commercial/dispatcher.py` |
| Тексты/кнопки подписчика | `telegram/commercial_texts.py`, `user_keyboards.py`, `user_handlers.py` |

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
