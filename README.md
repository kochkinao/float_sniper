# CS2 Steam Float Sniper

Read-only Telegram-бот для мониторинга новых лотов CS2 на Steam Community Market.

Бот **ничего не покупает**, не логинится в Steam и не использует cookies. Он только ищет кандидатов и присылает аналитику для ручного решения.

---

## Стратегия

Главная идея:

```text
1. На Steam появляется скин с красивым float.
2. Продавец часто не понимает ценность float и ставит обычную Steam-цену.
3. Бот сравнивает цену лота с обычной видимой Steam-ценой.
4. Потом бот идёт на CSFloat и смотрит:
   - обычный CSFloat рынок;
   - похожие float;
   - продажи похожих float;
   - premium за float;
   - потенциальный ROI после комиссии CSFloat.
5. Если есть шанс перепродать дороже за счёт float-premium — бот отправляет алерт.
```

Важно: обычный CSFloat рынок может быть дешевле Steam. Это нормально. Мы не ищем простой арбитраж `CSFloat floor > Steam price`. Мы ищем **недооценённый красивый float на Steam**.

---

## Как работает бот

```text
watchlist → Steam scan → новые listing id → float scoring → CSFloat analytics → Telegram alert
```

По шагам:

1. Бот берёт `watchlist` из `config.json`.
2. Каждую минуту открывает публичные страницы Steam Market.
3. Парсит видимые лоты: цена, float, listing id, ссылка.
4. Проверяет, красивый ли float.
5. Для кандидатов делает CSFloat-анализ.
6. Применяет фильтры ROI/premium/tier.
7. Отправляет алерт в Telegram.

---

## Как читать алерт

### Steam-блок

```text
Steam рынок:
- обычная видимая цена Steam: $5.00
- кандидат к Steam median: +0.0%
```

Показывает, стоит ли кандидат примерно как обычный лот на Steam.

Хорошо: кандидат около обычной цены.  
Плохо: кандидат уже сильно дороже обычной цены.

---

### Float-блок

```text
Float: 0.55551234
Tier/score: 🔥 A / 61
```

Показывает, почему бот заинтересовался.

Тиры:

| Tier | Значение |
|---|---|
| 💎 S | очень сильный/редкий float |
| 🔥 A | сильный float |
| ✨ B | средний, может быть интересен |
| 👀 C | слабый, часто шум |

---

### CSFloat-блок

```text
CSFloat рынок:
- обычный CSFloat рынок: floor $4.20; median10 $4.50
- похожие float: ±0.005 | n=8 | median $6.20
- продажи похожих float: ±0.01 | n=3 | median $5.90
- премия за float к обычному CSFloat рынку: +31.1%
```

Главные поля:

| Поле | Что значит |
|---|---|
| `обычный CSFloat рынок` | цена обычного такого скина на CSFloat |
| `похожие float` | активные лоты рядом с float кандидата |
| `продажи похожих float` | реальные продажи рядом с float |
| `n` | сколько данных найдено |
| `median` | середина цен, устойчивее к неадекватным лотам |
| `premium` | насколько похожие float дороже обычного рынка |

---

### Перепродажа

```text
Перепродажа на CSFloat:
- target sell: $5.90
- break-even: $5.10
- для +10% ROI: $5.61; для +20%: $6.12
- ожидаемый ROI после 2% CSFloat: +15.6%
```

Это основной блок принятия решения.

| Поле | Что значит |
|---|---|
| `target sell` | примерная цена продажи на CSFloat |
| `break-even` | цена без убытка после комиссии CSFloat |
| `для +10% ROI` | цена продажи для +10% прибыли |
| `ROI` | потенциальная прибыль после 2% комиссии CSFloat |

---

## Настройки

Основные настройки в Telegram `/settings`:

| Раздел | Что настраивает |
|---|---|
| 💰 Цена Steam | min/max цена лота Steam |
| 🏷 Тиры float | какие tier пропускать: S/A/B/C |
| 📈 ROI/премия | ROI, premium, max Steam overpay |
| 🔢 Красивый float | min score и low-float threshold |
| ⏱ Время и антиспам | интервал скана и задержка алертов |
| 📖 Гайд | объяснение стратегии прямо в боте |

Текущая логика фильтров:

```text
allowed_tiers = S,A,B
min_resale_roi_pct = 10
min_float_premium_pct = 10
max_steam_overpay_pct = 10
```

То есть бот старается не слать:

- слабые C-tier сигналы;
- сделки с ROI ниже 10%;
- лоты без хотя бы 10% premium за float;
- Steam-лоты, которые уже сильно дороже обычной Steam-цены.

---

## Telegram-команды

| Команда | Назначение |
|---|---|
| `/menu` | главное меню |
| `/settings` | настройки |
| `/status` | статус мониторинга |
| `/watchlist` | получить sorted CSV watchlist |
| `/last` | последние сигналы |
| `/scan` | ручной скан |
| `/pause` / `/resume` | пауза/старт |
| `/allow user_id` | админ: добавить пользователя |
| `/revoke user_id` | админ: удалить пользователя |
| `/whitelist` | список разрешённых пользователей |

---

## Watchlist через файл

Команда:

```text
/watchlist
```

Бот отправляет CSV:

```csv
market_hash_name
AK-47 | Slate (Factory New)
AWP | Atheris (Factory New)
```

Можно отредактировать файл и отправить обратно. Новые скины бот сначала проверит на Steam.

---

## Файлы проекта

| Файл | Назначение |
|---|---|
| `steam_alert_bot.py` | Telegram bot + scanner daemon |
| `steam_market_watcher.py` | Steam Market parser |
| `beautiful_float.py` | scoring красивого float |
| `csfloat_evaluator.py` | CSFloat analytics |
| `config.json` | настройки и watchlist |
| `steam_alert_bot.sqlite` | seen listings, whitelist, alerts history |
| `output/steam_alert_bot.log` | лог |
| `ALERT_ANALYTICS_GUIDE.md` | шпаргалка по чтению алертов |

---

## Короткая формула

```text
Хороший сигнал = обычная Steam цена + красивый float + CSFloat premium + ROI после комиссии
```
