# Optimization roadmap для CS2 Steam Float Sniper

## Цель

Сделать систему быстрее и масштабируемее, чтобы можно было расширять watchlist и потом подключать другие площадки: CSFloat, Lis-Skins, BUFF, CS.MONEY и т.д.

Главный принцип: **система Григория/Syncbox не должна страдать**. CS2-мониторинг должен быть отдельным, изолированным и управляемым по лимитам.

---

## Текущая проблема

Сейчас watchlist большой:

```text
164 предмета
```

Если проверять их последовательно, даже по 0.3 секунды на предмет, это уже около 50 секунд только на паузы. Реально больше из-за:

- задержек Steam;
- таймаутов;
- парсинга;
- CSFloat-аналитики;
- Telegram-отправки.

Поэтому полный цикл мог растягиваться на 2–5 минут.

---

## Что уже сделано

### 1. Параллельная проверка Steam

Добавлен параметр:

```text
steam_workers
```

Он означает: сколько Steam-страниц проверять одновременно.

Режимы:

| Значение | Режим |
|---:|---|
| 3 | осторожно, меньше риск лимитов |
| 6 | нормальный баланс |
| 10 | агрессивно, быстрее, но выше риск таймаутов/лимитов |

---

### 2. Таймаут Steam

Добавлен параметр:

```text
steam_timeout_seconds
```

Если Steam-страница долго не отвечает, бот не зависает на ней слишком долго.

---

### 3. CSFloat cache

Добавлен параметр:

```text
csfloat_cache_ttl_seconds
```

Бот кэширует CSFloat-аналитику по:

```text
item name + округлённый float bucket
```

Это снижает количество повторных CSFloat-запросов.

---

### 4. Steam overpay pre-filter

Если кандидат уже слишком дорогой относительно обычной Steam-цены, бот не идёт в CSFloat.

Это экономит время.

### 5. Health/reject observability

Добавлены:

```text
scan_metrics SQLite
Health / почему 0
Health 1h / 24h
auto-analysis recommendation
reject reasons
background manual scan
auto-safe mode on Steam degradation
```

Теперь `alerts 0` объясняется причинами: Steam source empty/degraded, already seen, weak float score, Steam overpay, ROI/premium фильтры и т.д. Если несколько сканов подряд показывают деградацию Steam, бот временно снижает workers и interval, чтобы не усиливать проблему.

---

## Как правильно масштабировать дальше

### Phase 2. Watchlist Groups — начато

Добавлено:

```text
watchlist_groups в config
core group из старого watchlist
/groups и 📁 Groups в Telegram
включить/выключить группу
ручной scan группы
интервал 60s / 5m на группу
scanner loop сканирует due-группы отдельно
```

Цель: вынести важные предметы в `core` и проверять чаще, а дешёвые/шумные — реже. Следующий шаг: авто-разбиение по ликвидности/цене и более удобный import wizard.

### Phase 2.1. Group CSV import/export — добавлено

Формат:

```csv
group,enabled,interval_seconds,market_hash_name
core,True,60,AK-47 | Slate (Factory New)
cheap,True,300,Glock-18 | Clear Polymer (Field-Tested)
premium,True,120,AWP | ...
test,False,300,...
```

В Telegram добавлено:

```text
/groups
/groupfile
📁 Groups → Export groups CSV
📁 Groups → Default groups
```

При загрузке grouped CSV бот применяет diff, сохраняет группы, проверяет новые market_hash_name через Steam и ограничивает массовую проверку первыми 200 новыми предметами, чтобы не забить источник.

### Phase 1.4. Steam empty diagnostics — добавлено

Добавлено:

```text
empty_reason
http_status
html_size
page_title
listinginfo/listing_row flags
captcha/rate_limit/access_denied/login flags
sanitized HTML samples в output/debug/steam_empty_samples/
empty reasons в Health и логах
```

Цель: отличать реальную пустоту Steam от parser miss / rate limit / captcha / short HTML / consent/login page. Samples сохраняются без cookies и с редакцией потенциальных токенов.

### Phase 1.5. Rate-limit aware controller — добавлено

Если `empty_reason=rate_limited` занимает большую долю страниц группы, бот теперь ставит конкретную группу в cooldown:

```text
RATE LIMIT COOLDOWN group core: rate_limited 164/164 pages (100%); cooldown 300s; strikes 1
```

Повторные срабатывания увеличивают cooldown экспоненциально до max. В `/groups` показывается `cooldown ...`, а scanner пропускает группу до окончания cooldown. Это лучше, чем продолжать бить Steam и усиливать ограничение.

### Phase 1.6. Persistent cooldown state — добавлено

Cooldown теперь хранится в SQLite (`group_cooldowns`), а не только в памяти процесса:

```text
group_name
until_ts
reason
strikes
duration
updated_ts
```

При старте бот очищает истёкшие cooldown, восстанавливает активные и логирует `Restored group cooldowns`. В `/groups` добавлена кнопка `🧊 clear`, чтобы вручную снять cooldown с конкретной группы.

### Phase 2.2. Auto-grouping draft/apply — добавлено

Бот умеет строить авто-разбиение без новых Steam-запросов, по уже увиденным ценам из SQLite `seen`:

```text
cheap: median_price < $3, interval 300s
core: $3–50 и достаточно наблюдений, interval 60s
premium: > $50, interval 180s
test: нет цены/мало наблюдений, interval 900s
```

В `/groups` добавлено:

```text
🧠 Auto draft
✅ Apply auto groups
```

Также добавлены команды `/autogroups` и `/apply_autogroups`. После применения текущий watchlist разложен так:

```text
core 6
cheap 74
premium 0
test 84
```

Чтобы не сканировать все due-группы залпом после рестарта/изменения групп, добавлен `max_groups_per_cycle=1`: scanner берёт только одну due-группу за цикл.

### Phase 2.3. Chunked group scanner — добавлено

Группа больше не сканируется целиком, если в ней много items. У каждой группы есть `chunk_size`:

```text
core: chunk 6/6
cheap: chunk 20/74
test: chunk 10/84
premium: chunk 10
```

Состояние курсора хранится в SQLite `group_scan_state`, поэтому бот продолжает следующий chunk после рестарта:

```text
group_name
cursor
updated_ts
```

Логи теперь показывают chunk:

```text
Скан: group cheap chunk 1-20/74 20 ... empty rate_limited:20
```

В `/groups` показывается `chunk N/total next X`, а в UI есть кнопки `ch 10`, `ch 20`, `ch all`. Это финально убирает burst внутри больших групп.

## Этап 1. Локальная оптимизация без прокси/VPN

Это уже начато.

Рекомендуемые настройки:

```text
steam_workers = 6
steam_timeout_seconds = 12
csfloat_cache_ttl_seconds = 600
```

Если Steam часто даёт таймауты:

```text
steam_workers = 3
steam_timeout_seconds = 8–12
```

Если всё стабильно и нужно быстрее:

```text
steam_workers = 10
steam_timeout_seconds = 8–12
```

---

## Этап 2. Watchlist groups

Watchlist нужно разбить на группы:

```text
core
cheap
mid
premium
test
```

Или по источникам/логике:

```text
liquid_rifles
liquid_awp
cheap_float_patterns
new_collections
manual_test
```

Зачем:

- разным группам можно дать разную частоту;
- важные скины проверять чаще;
- дешёвый шум проверять реже;
- удобно управлять через Telegram/UI.

Пример:

| Группа | Частота | Назначение |
|---|---:|---|
| core | 30–60s | самые интересные ликвидные скины |
| cheap | 2–5m | дешёвые тестовые скины |
| premium | 1–3m | дорогие, осторожно |
| test | вручную | эксперименты |

---

## Этап 3. Очередь задач

Нужно разделить систему на две очереди:

```text
Steam fetch queue
CSFloat analytics queue
```

Пайплайн:

```text
Steam worker нашёл новый лот
→ если float красивый и Steam overpay нормальный
→ задача уходит в CSFloat queue
→ CSFloat worker считает premium/ROI
→ Telegram alert
```

Плюс: Telegram-интерфейс всегда быстрый, потому что тяжёлый анализ живёт отдельно.

---

## Этап 4. Прокси/VPN pool

Можно использовать только аккуратно и отдельно от важных систем.

Правильная архитектура:

```text
proxy_pool.json
worker_id → proxy/vpn endpoint
rate_limit per endpoint
healthcheck per endpoint
cooldown on errors
```

Важно:

- не смешивать с Syncbox/финансовой системой;
- отдельный процесс/контейнер;
- отдельные лимиты CPU/RAM/network;
- отдельные логи;
- если endpoint нестабилен — отключать его автоматически;
- не использовать это для нарушения правил площадок/бан-эвейжна; лучше держаться в разумных rate limits.

---

## Этап 5. Web UI

Будущая морда:

- группы watchlist;
- настройки частоты по группам;
- фильтры ROI/premium/overpay;
- история сигналов;
- статус worker/proxy;
- ручная проверка конкретного item;
- сравнение площадок.

---

## Будущая мультиплощадочная система

Потенциальный пайплайн:

```text
Steam / CSFloat / Lis-Skins / BUFF / CS.MONEY watchers
→ normalized item model
→ price comparison engine
→ float/sticker/pattern evaluator
→ opportunity scorer
→ Telegram/Web UI
→ manual buy link
```

Покупки лучше оставлять ручными до тех пор, пока статистика сигналов не будет доказана.

---

## Метрики, которые нужно собирать

Чтобы понимать, где тормозит:

```text
scan_elapsed_seconds
steam_success_count
steam_timeout_count
steam_error_count
csfloat_requests
csfloat_cache_hits
alerts_count
watchlist_size
```

Эти метрики потом можно вывести в `/status`.
