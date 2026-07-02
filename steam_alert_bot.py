#!/usr/bin/env python3
"""Telegram-configurable Steam float watcher.

Read-only: no Steam login, no cookies, no buying.
Secrets: reads STEAM_ALERT_BOT_TOKEN from /home/hermes/.config/cs2-float-sniper/.env.

UX v0.4:
- admin/whitelist access control;
- watchlist export/import through sorted CSV/TXT files;
- no useless per-item delete buttons;
- concrete alerts are sent as plain text to avoid Telegram Markdown errors;
- /last shows recent concrete signals.
"""
from __future__ import annotations

import concurrent.futures
from collections import Counter, defaultdict, deque
import csv
import io
import json
import mimetypes
import re
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

BASE = Path('/home/hermes/cs2-float-sniper')
CONFIG_PATH = BASE / 'config.json'
ENV_PATH = Path('/home/hermes/.config/cs2-float-sniper/.env')
DB_PATH = BASE / 'steam_alert_bot.sqlite'
LOG_PATH = BASE / 'output' / 'steam_alert_bot.log'
OUT_DIR = BASE / 'output'
EXPORT_DIR = OUT_DIR / 'watchlists'
DEBUG_DIR = OUT_DIR / 'debug' / 'steam_empty_samples'
ADMIN_USER_ID = 498975827

sys.path.insert(0, str(BASE))
from steam_market_watcher import fetch_html, parse_listings, SteamListing, steam_url  # type: ignore
from csfloat_evaluator import CSFloat, load_api_key, evaluate_nearby_float  # type: ignore

ALLOWED_SET_KEYS = {
    'interval_seconds': int,
    'steam_workers': int,
    'steam_timeout_seconds': int,
    'auto_safe_mode_enabled': bool,
    'safe_mode_workers': int,
    'safe_mode_interval_seconds': int,
    'safe_mode_duration_seconds': int,
    'safe_mode_empty_rate_pct': int,
    'rate_limit_controller_enabled': bool,
    'rate_limit_cooldown_seconds': int,
    'rate_limit_cooldown_max_seconds': int,
    'rate_limit_cooldown_multiplier': float,
    'rate_limit_empty_rate_pct': int,
    'rate_limit_safe_workers': int,
    'max_groups_per_cycle': int,
    'default_group_chunk_size': int,
    'min_price_usd': float,
    'max_price_usd': float,
    'min_beautiful_score': int,
    'low_float_threshold': float,
    'min_resale_roi_pct': float,
    'min_float_premium_pct': float,
    'max_steam_overpay_pct': float,
    'alert_send_delay_seconds': float,
    'csfloat_cache_ttl_seconds': int,
    'send_all_new': bool,
    'enabled': bool,
}


def log(msg: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n"
    LOG_PATH.open('a', encoding='utf-8').write(line)
    print(line, end='', flush=True)


def sanitize_html_sample(html: str) -> str:
    # Public Steam pages should not include secrets, but strip common risky patterns anyway.
    text = re.sub(r'(sessionid|steamLoginSecure|steamRememberLogin|webTradeEligibility)[^;\s<]+', r'\1=[REDACTED]', html, flags=re.I)
    return text[:200_000]


def classify_empty_html(html: str, status: int | None = None) -> dict[str, Any]:
    low = html.lower()
    title_m = re.search(r'<title[^>]*>(.*?)</title>', html, flags=re.I | re.S)
    title = re.sub(r'\s+', ' ', title_m.group(1)).strip()[:120] if title_m else ''
    flags = {
        'has_listinginfo': 'g_rglistinginfo' in low,
        'has_listing_row': 'market_listing_row' in low,
        'has_app_730': 'counter-strike 2' in low or '/730/' in low,
        'has_captcha': 'captcha' in low or 'recaptcha' in low,
        'has_rate_limit': 'too many requests' in low or 'rate limit' in low or '429' in low,
        'has_access_denied': 'access denied' in low or 'temporarily unavailable' in low,
        'has_login': 'login' in low and 'steam' in low,
    }
    if status and status >= 500:
        reason = 'http_5xx'
    elif status == 429 or flags['has_rate_limit']:
        reason = 'rate_limited'
    elif flags['has_captcha']:
        reason = 'captcha'
    elif flags['has_access_denied']:
        reason = 'access_denied_or_unavailable'
    elif len(html) < 5000:
        reason = 'short_html'
    elif flags['has_listinginfo'] or flags['has_listing_row']:
        reason = 'parser_miss_or_empty_listinginfo'
    elif flags['has_login']:
        reason = 'login_or_consent_page'
    else:
        reason = 'unknown_empty_html'
    return {'reason': reason, 'status': status, 'html_size': len(html), 'title': title, **flags}


def fetch_steam_html_meta(market_hash_name: str, timeout: int = 25) -> tuple[str, dict[str, Any]]:
    req = urllib.request.Request(
        steam_url(market_hash_name),
        headers={
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/125 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        html = raw.decode('utf-8', 'replace')
        meta = {'status': getattr(r, 'status', None), 'html_size': len(html), 'url': r.geturl()}
        return html, meta


def save_empty_sample(name: str, html: str, diag: dict[str, Any]) -> str | None:
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r'[^a-zA-Z0-9_.-]+', '_', name)[:80]
        tag = time.strftime('%Y%m%d_%H%M%S')
        base = DEBUG_DIR / f'{tag}_{safe_name}'
        html_path = base.with_suffix('.html')
        json_path = base.with_suffix('.json')
        html_path.write_text(sanitize_html_sample(html), encoding='utf-8')
        json_path.write_text(json.dumps(diag, ensure_ascii=False, indent=2), encoding='utf-8')
        return str(html_path)
    except Exception as e:
        log(f'WARN save_empty_sample failed: {type(e).__name__}: {e}')
        return None


def load_env() -> dict[str, str]:
    data = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            data[k.strip()] = v.strip().strip('"').strip("'")
    return data


def load_config() -> dict[str, Any]:
    cfg = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
    ensure_watchlist_groups(cfg)
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    ensure_watchlist_groups(cfg)
    tmp = CONFIG_PATH.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.replace(CONFIG_PATH)


def ensure_watchlist_groups(cfg: dict[str, Any]) -> None:
    groups = cfg.get('watchlist_groups')
    if not isinstance(groups, dict) or not groups:
        cfg['watchlist_groups'] = {
            'core': {
                'enabled': True,
                'interval_seconds': int(cfg.get('interval_seconds', 60)),
                'items': cfg.get('watchlist', []),
                'description': 'основная группа, создана из старого watchlist',
            }
        }
    # Keep legacy watchlist as union for backward compatibility/export.
    union: list[str] = []
    seen = set()
    for g in cfg.get('watchlist_groups', {}).values():
        if not isinstance(g, dict):
            continue
        g.setdefault('enabled', True)
        g.setdefault('interval_seconds', int(cfg.get('interval_seconds', 60)))
        g.setdefault('chunk_size', int(cfg.get('default_group_chunk_size', 20)))
        items = g.get('items') or []
        g['items'] = clean_names([str(x) for x in items]) if 'clean_names' in globals() else [str(x).strip() for x in items if str(x).strip()]
        for x in g['items']:
            if x not in seen:
                seen.add(x); union.append(x)
    cfg['watchlist'] = sorted(union, key=lambda s: s.lower())


def watchlist_groups(cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    ensure_watchlist_groups(cfg)
    return cfg.get('watchlist_groups', {})


def group_items(cfg: dict[str, Any], group: str) -> list[str]:
    g = watchlist_groups(cfg).get(group, {})
    return sorted(clean_names(g.get('items', [])), key=lambda s: s.lower())


def sorted_watchlist(cfg: dict[str, Any]) -> list[str]:
    seen = set()
    out = []
    for x in cfg.get('watchlist', []):
        x = str(x).strip()
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return sorted(out, key=lambda s: s.lower())


def init_db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    con.execute('''CREATE TABLE IF NOT EXISTS seen (
        listingid TEXT PRIMARY KEY,
        first_seen_ts INTEGER NOT NULL,
        source_query TEXT,
        market_hash_name TEXT,
        price_usd REAL,
        float_value REAL,
        beautiful_tier TEXT,
        beautiful_score INTEGER
    )''')
    con.execute('''CREATE TABLE IF NOT EXISTS chats (
        chat_id INTEGER PRIMARY KEY,
        first_seen_ts INTEGER NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1
    )''')
    con.execute('''CREATE TABLE IF NOT EXISTS whitelist (
        user_id INTEGER PRIMARY KEY,
        added_by INTEGER,
        added_ts INTEGER NOT NULL
    )''')
    con.execute('''CREATE TABLE IF NOT EXISTS alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER NOT NULL,
        listingid TEXT,
        source_query TEXT,
        market_hash_name TEXT,
        price_usd REAL,
        float_value REAL,
        beautiful_tier TEXT,
        beautiful_score INTEGER,
        beautiful_reasons TEXT,
        url TEXT
    )''')
    con.execute('''CREATE TABLE IF NOT EXISTS scan_metrics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER NOT NULL,
        health TEXT,
        watchlist INTEGER,
        workers INTEGER,
        observed INTEGER,
        new_count INTEGER,
        candidates INTEGER,
        alerts INTEGER,
        elapsed REAL,
        source_json TEXT,
        rejects_json TEXT
    )''')
    con.execute('''CREATE TABLE IF NOT EXISTS group_cooldowns (
        group_name TEXT PRIMARY KEY,
        until_ts REAL NOT NULL,
        reason TEXT,
        strikes INTEGER NOT NULL DEFAULT 0,
        duration INTEGER NOT NULL DEFAULT 0,
        updated_ts INTEGER NOT NULL
    )''')
    con.execute('''CREATE TABLE IF NOT EXISTS group_scan_state (
        group_name TEXT PRIMARY KEY,
        cursor INTEGER NOT NULL DEFAULT 0,
        updated_ts INTEGER NOT NULL
    )''')
    con.execute('INSERT OR IGNORE INTO whitelist(user_id, added_by, added_ts) VALUES (?, ?, ?)', (ADMIN_USER_ID, ADMIN_USER_ID, int(time.time())))
    con.commit()
    return con


def parse_bool(raw: str) -> bool:
    return raw.lower() in ('1', 'true', 'yes', 'да', 'on', 'y')


def translate_reasons(reasons: str | None) -> str:
    if not reasons:
        return 'низкий float / нужна ручная проверка'
    out = []
    for part in reasons.split(';'):
        s = part.strip()
        m = re.match(r'(\d+)x repeated digit (\d) \(\+(\d+)\)', s)
        if m:
            out.append(f'{m.group(1)} повторения цифры {m.group(2)} (+{m.group(3)})'); continue
        m = re.match(r'meme pattern (\d+) \(\+(\d+)\)', s)
        if m:
            out.append(f'мемный паттерн {m.group(1)} (+{m.group(2)})'); continue
        m = re.match(r'(\d+) leading zeros after decimal \(\+(\d+)\)', s)
        if m:
            out.append(f'{m.group(1)} нулей после запятой (+{m.group(2)})'); continue
        m = re.match(r'main repeat appears early \(\+(\d+)\)', s)
        if m:
            out.append(f'основной повтор стоит рано (+{m.group(1)})'); continue
        m = re.match(r'ascending sequence (\d+) \(\+(\d+)\)', s)
        if m:
            out.append(f'восходящая последовательность {m.group(1)} (+{m.group(2)})'); continue
        m = re.match(r'descending sequence (\d+) \(\+(\d+)\)', s)
        if m:
            out.append(f'нисходящая последовательность {m.group(1)} (+{m.group(2)})'); continue
        m = re.match(r'palindrome-like digits (.*?) \(\+(\d+)\)', s)
        if m:
            out.append(f'похоже на палиндром {m.group(1)} (+{m.group(2)})'); continue
        out.append(s)
    return '; '.join(out)


def tier_badge(tier: str | None, score: int | None) -> str:
    t = (tier or '').upper()
    if t == 'S': return '💎 S'
    if t == 'A': return '🔥 A'
    if t == 'B': return '✨ B'
    if t == 'C': return '👀 C'
    if score is not None and score >= 55: return '🔥 A'
    if score is not None and score >= 35: return '✨ B'
    return tier or 'normal'


def fmt_usd(v: Any) -> str:
    return f'${v:.2f}' if isinstance(v, (int, float)) else '$—'


def valuation_source_ru(src: Any) -> str:
    return {
        'recent_sales_near_float_median': 'медиана недавних продаж рядом с float',
        'active_near_float_median': 'медиана активных лотов рядом с float',
        'active_general_median_10_fallback': 'медиана первых 10 активных лотов, fallback',
    }.get(src, str(src) if src else 'нет')


def fmt_pct(v: Any) -> str:
    return f'{v:+.1f}%' if isinstance(v, (int, float)) else '—'



def human_bool(v: Any, yes: str = 'да', no: str = 'нет') -> str:
    if v is True:
        return yes
    if v is False:
        return no
    return '—'


def confidence_label(ev: dict[str, Any] | None) -> str:
    if not ev:
        return 'низкая'
    sales_n = (ev.get('recent_sale_stats') or {}).get('count', 0)
    active_n = (ev.get('active_comp_stats') or {}).get('count', 0)
    if sales_n >= 3 and active_n >= 5:
        return 'хорошая'
    if active_n >= 5:
        return 'средняя'
    return 'низкая'


def human_risks(raw: Any) -> str:
    text = str(raw or '').strip()
    if not text or text in ('—', '-'):
        return 'явных красных флагов нет'
    mapping = {
        'МАЛО АКТИВНЫХ КОМПОВ': 'мало активных похожих лотов',
        'МАЛО ПРОДАЖ РЯДОМ С FLOAT': 'мало продаж рядом с этим float',
        'НИЗКАЯ ЛИКВИДНОСТЬ': 'низкая ликвидность',
        'ROI МЕНЬШЕ 10%': 'ROI ниже 10%',
        'ПРЕМИЯ ЗА FLOAT МЕНЬШЕ 10%': 'премия за float слабая',
        'STEAM ДОРОЖЕ CSFLOAT-БАЗЫ С УЧЕТОМ НАЦЕНКИ': 'Steam цена выше нормальной Steam-наценки к CSFloat базе',
    }
    parts = [x.strip() for x in re.split(r'[;,]', text) if x.strip()]
    return '; '.join(mapping.get(x, x) for x in parts) if parts else text


def csfloat_summary(client: CSFloat | None, name: str | None, target_float: float | None, buy_price: float | None, ev: dict[str, Any] | None = None) -> str:
    if ev is None:
        if not client or not name:
            return 'CSFloat: не проверялся'
        try:
            ev = evaluate_nearby_float(client, name, target_float, buy_price, min_comps=5, limit=20)
        except Exception as e:
            return f'CSFloat: ошибка проверки ({type(e).__name__})'

    active = ev.get('active_comp_stats') or {}
    sales = ev.get('recent_sale_stats') or {}
    base_sell = ev.get('base_target_sell_price_usd') or ev.get('target_sell_price_usd') or ev.get('estimated_fair_usd')
    expected_sell = ev.get('target_sell_price_usd') or ev.get('estimated_fair_usd')
    beauty_mult = ev.get('beauty_multiplier')
    base_roi = ev.get('base_resale_roi_pct')
    roi = ev.get('resale_roi_pct')
    steam_ok = ev.get('steam_base_price_ok')

    lines = []
    lines.append('📌 Коротко:')
    lines.append(f'- Steam покупка: {fmt_usd(buy_price)}')
    lines.append(f'- База CSFloat рядом с таким float: {fmt_usd(base_sell)}')
    if beauty_mult is not None:
        lines.append(f'- Коэффициент красоты: x{float(beauty_mult):.2f}')
    lines.append(f'- Цель продажи на CSFloat: {fmt_usd(expected_sell)}')
    lines.append(f'- ROI после 2% CSFloat: {fmt_pct(roi)}')

    lines.append('')
    lines.append('💸 Проверка цены:')
    if ev.get('steam_fair_min_usd') is not None and ev.get('steam_fair_max_usd') is not None:
        lines.append(f'- Нормальная Steam-вилка от CSFloat базы: {fmt_usd(ev.get("steam_fair_min_usd"))}–{fmt_usd(ev.get("steam_fair_max_usd"))}')
        lines.append(f'- Steam цена в этой вилке: {human_bool(steam_ok)}')
    elif ev.get('steam_visible_median_usd') is not None:
        lines.append(f'- Видимая медиана Steam: {fmt_usd(ev.get("steam_visible_median_usd"))}')
        lines.append(f'- Кандидат к Steam median: {fmt_pct(ev.get("steam_delta_to_median_pct"))}')
    else:
        lines.append('- Steam-вилка: нет данных')
    if base_roi is not None:
        lines.append(f'- ROI без премии за красоту: {fmt_pct(base_roi)}')

    lines.append('')
    lines.append('🔎 CSFloat-компы:')
    lines.append(f'- Обычный рынок: floor {fmt_usd(ev.get("active_floor_usd"))}; median10 {fmt_usd(ev.get("active_median_10_usd"))}')
    if ev.get('active_comp_window') is not None:
        lo = ev.get('active_comp_float_min')
        hi = ev.get('active_comp_float_max')
        line = f'- Активные похожие float: ±{ev.get("active_comp_window")} | n={active.get("count", 0)} | median {fmt_usd(active.get("median_usd"))}'
        if lo is not None and hi is not None:
            line += f' | {lo:.6f}–{hi:.6f}'
        lines.append(line)
    else:
        lines.append('- Активные похожие float: мало данных')
    if ev.get('recent_sale_window') is not None:
        lines.append(f'- Продажи похожих float: ±{ev.get("recent_sale_window")} | n={sales.get("count", 0)} | median {fmt_usd(sales.get("median_usd"))}')
    else:
        lines.append(f'- Продажи похожих float: мало данных ({ev.get("recent_sales_sample_total", 0)} продаж всего в истории)')

    lines.append('')
    lines.append('⚠️ Риски:')
    lines.append(f'- Доверие к оценке: {confidence_label(ev)}')
    lines.append(f'- {human_risks(ev.get("risk_flags"))}')
    lines.append(f'- Break-even на CSFloat: {fmt_usd(ev.get("break_even_sell_price_usd"))}')
    lines.append(f'- Цена для +10% ROI: {fmt_usd(ev.get("sell_price_for_10pct_roi_usd"))}; для +20% ROI: {fmt_usd(ev.get("sell_price_for_20pct_roi_usd"))}')
    return '\n'.join(lines)


def decision_label(ev: dict[str, Any] | None) -> str:
    if not ev:
        return '✅ Вывод: WATCH — CSFloat не проверен, нужна ручная оценка.'
    roi = ev.get('resale_roi_pct')
    base_roi = ev.get('base_resale_roi_pct')
    steam_ok = ev.get('steam_base_price_ok')
    beauty_score = ev.get('beautiful_score')
    beauty_mult = ev.get('beauty_multiplier')
    sales_n = (ev.get('recent_sale_stats') or {}).get('count', 0)
    active_n = (ev.get('active_comp_stats') or {}).get('count', 0)

    beauty_ok = isinstance(beauty_score, (int, float)) and beauty_score >= 55
    base_price_ok = steam_ok is not False

    if beauty_ok and base_price_ok:
        if isinstance(roi, (int, float)) and roi >= 25 and active_n >= 5:
            return '🚀 Вывод: STRONG — красивый float, цена выглядит базовой, есть хороший запас. Быстрая ручная проверка.'
        return '✅ Вывод: GEM WATCH — владелец, похоже, не заложил премию за красивый float. Проверить руками быстро.'

    if steam_ok is False:
        return '⚠️ Вывод: WATCH/SKIP — Steam цена выше нормальной вилки к CSFloat базе. Брать только если красота реально редкая.'
    if not isinstance(roi, (int, float)):
        return '⚠️ Вывод: WATCH — мало данных для расчета ROI.'
    if roi < 10:
        return '⚠️ Вывод: WATCH — по расчетной продаже ROI слабый, но можно смотреть вручную, если float очень красивый.'
    if roi < 25 or sales_n < 3 or active_n < 5:
        return '✅ Вывод: WATCH — идея есть, но данных/запаса мало. Нужна ручная проверка.'
    if roi >= 50 and sales_n >= 3 and active_n >= 5:
        return '🚀 Вывод: STRONG — высокий расчетный ROI, проверь руками быстро.'
    return '✅ Вывод: GOOD — есть запас на перепродажу, нужна ручная проверка.'


def plain_alert(r: SteamListing, csfloat_client: CSFloat | None = None, ev: dict[str, Any] | None = None) -> str:
    badge = tier_badge(r.beautiful_tier, r.beautiful_score)
    reasons = translate_reasons(r.beautiful_reasons)
    title = r.market_hash_name or r.source_query or ''
    return (
        f"🔥 Найден кандидат · {badge}\n"
        f"{title}\n\n"
        f"🎯 Почему интересно:\n"
        f"- Float: {r.float_value if r.float_value is not None else '—'}\n"
        f"- Красота: {badge} / score {r.beautiful_score if r.beautiful_score is not None else '—'}\n"
        f"- Причина: {reasons}\n\n"
        f"💰 Лот Steam:\n"
        f"- Цена: {fmt_usd(r.price_usd)}\n"
        f"- Listing ID: {r.listingid}\n"
        f"- Ссылка: {r.url}\n\n"
        f"{csfloat_summary(csfloat_client, r.market_hash_name, r.float_value, r.price_usd, ev)}\n\n"
        f"{decision_label(ev)}\n\n"
        "Действие: бот не покупает. Открой лот, проверь float/скин/ликвидность и только потом принимай решение."
    )


class TelegramBot:
    def __init__(self, token: str, con: sqlite3.Connection):
        self.token = token
        self.base = f'https://api.telegram.org/bot{token}'
        self.file_base = f'https://api.telegram.org/file/bot{token}'
        self.con = con
        self.offset = 0

    def api(self, method: str, payload: dict[str, Any] | None = None, timeout: int = 30) -> dict[str, Any]:
        clean: dict[str, Any] = {}
        for k, v in (payload or {}).items():
            if isinstance(v, (dict, list)):
                clean[k] = json.dumps(v, ensure_ascii=False)
            elif isinstance(v, bool):
                clean[k] = 'true' if v else 'false'
            else:
                clean[k] = v
        data = urllib.parse.urlencode(clean).encode('utf-8')
        req = urllib.request.Request(f'{self.base}/{method}', data=data, headers={'User-Agent': 'cs2-steam-alert-bot/0.4'})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode('utf-8'))

    def send(self, chat_id: int, text: str, disable_preview: bool = True, reply_markup: dict[str, Any] | None = None, parse_mode: str | None = 'Markdown') -> None:
        for i in range(0, len(text), 3900):
            payload: dict[str, Any] = {
                'chat_id': chat_id,
                'text': text[i:i+3900],
                'disable_web_page_preview': disable_preview,
            }
            if parse_mode:
                payload['parse_mode'] = parse_mode
            if i == 0 and reply_markup:
                payload['reply_markup'] = reply_markup
            try:
                self.api('sendMessage', payload, timeout=20)
            except Exception:
                # Most common reason: Markdown parse error from market names. Retry as plain text.
                payload.pop('parse_mode', None)
                self.api('sendMessage', payload, timeout=20)

    def send_document(self, chat_id: int, path: Path, caption: str = '') -> None:
        boundary = '----HermesCS2Boundary' + str(int(time.time() * 1000))
        mime = mimetypes.guess_type(str(path))[0] or 'application/octet-stream'
        body = io.BytesIO()
        def part(name: str, value: bytes, filename: str | None = None, content_type: str | None = None) -> None:
            body.write(f'--{boundary}\r\n'.encode())
            if filename:
                body.write(f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode())
                body.write(f'Content-Type: {content_type or "application/octet-stream"}\r\n\r\n'.encode())
                body.write(value)
                body.write(b'\r\n')
            else:
                body.write(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
                body.write(value)
                body.write(b'\r\n')
        part('chat_id', str(chat_id).encode())
        if caption:
            part('caption', caption.encode('utf-8'))
        part('document', path.read_bytes(), filename=path.name, content_type=mime)
        body.write(f'--{boundary}--\r\n'.encode())
        req = urllib.request.Request(f'{self.base}/sendDocument', data=body.getvalue(), headers={'Content-Type': f'multipart/form-data; boundary={boundary}', 'User-Agent': 'cs2-steam-alert-bot/0.4'})
        with urllib.request.urlopen(req, timeout=60) as r:
            json.loads(r.read().decode('utf-8'))

    def edit(self, chat_id: int, message_id: int, text: str, reply_markup: dict[str, Any] | None = None) -> None:
        payload = {'chat_id': chat_id, 'message_id': message_id, 'text': text[:3900], 'parse_mode': 'Markdown', 'disable_web_page_preview': True}
        if reply_markup:
            payload['reply_markup'] = reply_markup
        try:
            self.api('editMessageText', payload, timeout=20)
        except Exception:
            payload.pop('parse_mode', None)
            self.api('editMessageText', payload, timeout=20)

    def answer_callback(self, callback_id: str, text: str = '') -> None:
        self.api('answerCallbackQuery', {'callback_query_id': callback_id, 'text': text}, timeout=10)

    def is_allowed(self, user_id: int) -> bool:
        if user_id == ADMIN_USER_ID:
            return True
        return self.con.execute('SELECT 1 FROM whitelist WHERE user_id=?', (user_id,)).fetchone() is not None

    def register_chat(self, chat_id: int) -> None:
        self.con.execute('INSERT OR IGNORE INTO chats(chat_id, first_seen_ts, enabled) VALUES (?, ?, 1)', (chat_id, int(time.time())))
        self.con.execute('UPDATE chats SET enabled=1 WHERE chat_id=?', (chat_id,))
        self.con.commit()

    def enabled_chats(self) -> list[int]:
        return [r[0] for r in self.con.execute('SELECT chat_id FROM chats WHERE enabled=1').fetchall()]

    def main_keyboard(self, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
        cfg = cfg or load_config()
        enabled = bool(cfg.get('enabled', True))
        return {'inline_keyboard': [
            [{'text': '📊 Статус', 'callback_data': 'menu:status'}, {'text': '⚙️ Настройки', 'callback_data': 'menu:settings'}],
            [{'text': '🔍 Скан сейчас', 'callback_data': 'act:scan'}, {'text': '⏸ Пауза' if enabled else '▶️ Старт', 'callback_data': 'act:toggle'}],
            [{'text': '🧾 Последние сигналы', 'callback_data': 'menu:last'}, {'text': '🩺 Health / почему 0', 'callback_data': 'menu:health'}],
            [{'text': '📁 Groups', 'callback_data': 'menu:groups'}, {'text': '📄 Watchlist файл', 'callback_data': 'act:watchfile'}, {'text': '📖 Гайд', 'callback_data': 'menu:guide_main'}],
            [{'text': '❓ Помощь', 'callback_data': 'menu:help'}],
        ]}

    def health_keyboard(self, cfg: dict[str, Any]) -> dict[str, Any]:
        return {'inline_keyboard': [
            [{'text': '🩺 Последние сканы', 'callback_data': 'menu:health'}, {'text': '📊 1 час', 'callback_data': 'health:1'}, {'text': '📈 24 часа', 'callback_data': 'health:24'}],
            [{'text': '⚙️ Скорость', 'callback_data': 'settings:time'}, {'text': '🏠 Главное меню', 'callback_data': 'menu:main'}],
        ]}

    def groups_keyboard(self, cfg: dict[str, Any]) -> dict[str, Any]:
        rows = []
        for name, g in list(watchlist_groups(cfg).items())[:8]:
            enabled = bool(g.get('enabled', True))
            rows.append([
                {'text': f"{'✅' if enabled else '⏸'} {name}", 'callback_data': f'group:toggle:{name}'},
                {'text': '🔍 scan', 'callback_data': f'group:scan:{name}'},
                {'text': '🧊 clear', 'callback_data': f'group:clearcd:{name}'},
                {'text': '60s', 'callback_data': f'group:interval:{name}:60'},
                {'text': '5m', 'callback_data': f'group:interval:{name}:300'},
            ])
            rows.append([
                {'text': f"chunk {int(g.get('chunk_size', cfg.get('default_group_chunk_size', 20)))}", 'callback_data': f'group:noop:{name}'},
                {'text': 'ch 10', 'callback_data': f'group:chunk:{name}:10'},
                {'text': 'ch 20', 'callback_data': f'group:chunk:{name}:20'},
                {'text': 'ch all', 'callback_data': f'group:chunk:{name}:9999'},
            ])
        rows.append([{'text': '📄 Export groups CSV', 'callback_data': 'act:groupfile'}, {'text': '🧠 Auto draft', 'callback_data': 'act:autogroupdraft'}])
        rows.append([{'text': '✅ Apply auto groups', 'callback_data': 'act:applyautogroups'}, {'text': '➕ Default groups', 'callback_data': 'act:defaultgroups'}])
        rows.append([{'text': '📄 Экспорт all', 'callback_data': 'act:watchfile'}, {'text': '🏠 Главное меню', 'callback_data': 'menu:main'}])
        return {'inline_keyboard': rows}

    def settings_keyboard(self, cfg: dict[str, Any], section: str = 'main') -> dict[str, Any]:
        enabled = bool(cfg.get('enabled', True))
        if section == 'price':
            return {'inline_keyboard': [
                [{'text': 'min $0', 'callback_data': 'set:min_price_usd:0'}, {'text': 'min $1', 'callback_data': 'set:min_price_usd:1'}, {'text': 'min $5', 'callback_data': 'set:min_price_usd:5'}],
                [{'text': 'max $30', 'callback_data': 'set:max_price_usd:30'}, {'text': 'max $100', 'callback_data': 'set:max_price_usd:100'}, {'text': 'max $300', 'callback_data': 'set:max_price_usd:300'}],
                [{'text': '⬅️ Назад к настройкам', 'callback_data': 'menu:settings'}, {'text': '🏠 Главное меню', 'callback_data': 'menu:main'}],
            ]}
        if section == 'tier':
            tiers = {str(x).upper() for x in cfg.get('allowed_tiers', ['S','A','B'])}
            def mark(t: str) -> str:
                return ('✅ ' if t in tiers else '⬜️ ') + t
            return {'inline_keyboard': [
                [{'text': mark('S'), 'callback_data': 'tier:toggle:S'}, {'text': mark('A'), 'callback_data': 'tier:toggle:A'}, {'text': mark('B'), 'callback_data': 'tier:toggle:B'}, {'text': mark('C'), 'callback_data': 'tier:toggle:C'}],
                [{'text': 'Только S/A', 'callback_data': 'tier:preset:S,A'}, {'text': 'S/A/B', 'callback_data': 'tier:preset:S,A,B'}, {'text': 'Все тиры', 'callback_data': 'tier:preset:S,A,B,C'}],
                [{'text': '⬅️ Назад к настройкам', 'callback_data': 'menu:settings'}, {'text': '🏠 Главное меню', 'callback_data': 'menu:main'}],
            ]}
        if section == 'edge':
            return {'inline_keyboard': [
                [{'text': 'ROI ≥ 0%', 'callback_data': 'set:min_resale_roi_pct:0'}, {'text': 'ROI ≥ 10%', 'callback_data': 'set:min_resale_roi_pct:10'}, {'text': 'ROI ≥ 25%', 'callback_data': 'set:min_resale_roi_pct:25'}],
                [{'text': 'премия ≥ 0%', 'callback_data': 'set:min_float_premium_pct:0'}, {'text': 'премия ≥ 10%', 'callback_data': 'set:min_float_premium_pct:10'}, {'text': 'премия ≥ 25%', 'callback_data': 'set:min_float_premium_pct:25'}],
                [{'text': 'Steam overpay ≤ 5%', 'callback_data': 'set:max_steam_overpay_pct:5'}, {'text': '≤ 10%', 'callback_data': 'set:max_steam_overpay_pct:10'}, {'text': '≤ 25%', 'callback_data': 'set:max_steam_overpay_pct:25'}],
                [{'text': '⬅️ Назад к настройкам', 'callback_data': 'menu:settings'}, {'text': '🏠 Главное меню', 'callback_data': 'menu:main'}],
            ]}
        if section == 'float':
            return {'inline_keyboard': [
                [{'text': 'score ≥ 25', 'callback_data': 'set:min_beautiful_score:25'}, {'text': 'score ≥ 35', 'callback_data': 'set:min_beautiful_score:35'}, {'text': 'score ≥ 55', 'callback_data': 'set:min_beautiful_score:55'}],
                [{'text': 'LF ≤ 0.01', 'callback_data': 'set:low_float_threshold:0.01'}, {'text': 'LF ≤ 0.005', 'callback_data': 'set:low_float_threshold:0.005'}, {'text': 'LF ≤ 0.001', 'callback_data': 'set:low_float_threshold:0.001'}],
                [{'text': '⬅️ Назад к настройкам', 'callback_data': 'menu:settings'}, {'text': '🏠 Главное меню', 'callback_data': 'menu:main'}],
            ]}
        if section == 'time':
            return {'inline_keyboard': [
                [{'text': 'скан 30 сек', 'callback_data': 'set:interval_seconds:30'}, {'text': 'скан 60 сек', 'callback_data': 'set:interval_seconds:60'}, {'text': 'скан 120 сек', 'callback_data': 'set:interval_seconds:120'}],
                [{'text': 'workers 3', 'callback_data': 'set:steam_workers:3'}, {'text': 'workers 6', 'callback_data': 'set:steam_workers:6'}, {'text': 'workers 10', 'callback_data': 'set:steam_workers:10'}],
                [{'text': 'timeout 8s', 'callback_data': 'set:steam_timeout_seconds:8'}, {'text': '12s', 'callback_data': 'set:steam_timeout_seconds:12'}, {'text': '20s', 'callback_data': 'set:steam_timeout_seconds:20'}],
                [{'text': 'алерт delay 1s', 'callback_data': 'set:alert_send_delay_seconds:1'}, {'text': 'алерт delay 2s', 'callback_data': 'set:alert_send_delay_seconds:2'}, {'text': 'алерт delay 5s', 'callback_data': 'set:alert_send_delay_seconds:5'}],
                [{'text': 'CSFloat cache 5m', 'callback_data': 'set:csfloat_cache_ttl_seconds:300'}, {'text': '10m', 'callback_data': 'set:csfloat_cache_ttl_seconds:600'}, {'text': '30m', 'callback_data': 'set:csfloat_cache_ttl_seconds:1800'}],
                [{'text': 'auto-safe ON', 'callback_data': 'set:auto_safe_mode_enabled:true'}, {'text': 'auto-safe OFF', 'callback_data': 'set:auto_safe_mode_enabled:false'}],
                [{'text': 'safe workers 2', 'callback_data': 'set:safe_mode_workers:2'}, {'text': 'safe workers 3', 'callback_data': 'set:safe_mode_workers:3'}, {'text': 'safe 15m', 'callback_data': 'set:safe_mode_duration_seconds:900'}],
                [{'text': 'RL ctrl ON', 'callback_data': 'set:rate_limit_controller_enabled:true'}, {'text': 'RL ctrl OFF', 'callback_data': 'set:rate_limit_controller_enabled:false'}],
                [{'text': 'RL cooldown 5m', 'callback_data': 'set:rate_limit_cooldown_seconds:300'}, {'text': '15m', 'callback_data': 'set:rate_limit_cooldown_seconds:900'}, {'text': 'max 30m', 'callback_data': 'set:rate_limit_cooldown_max_seconds:1800'}],
                [{'text': '⬅️ Назад к настройкам', 'callback_data': 'menu:settings'}, {'text': '🏠 Главное меню', 'callback_data': 'menu:main'}],
            ]}
        return {'inline_keyboard': [
            [{'text': '💰 Цена Steam', 'callback_data': 'settings:price'}, {'text': '🏷 Тиры float', 'callback_data': 'settings:tier'}],
            [{'text': '📈 ROI/премия', 'callback_data': 'settings:edge'}, {'text': '🔢 Красивый float', 'callback_data': 'settings:float'}],
            [{'text': '⏱ Время и антиспам', 'callback_data': 'settings:time'}, {'text': '📖 Гайд', 'callback_data': 'menu:guide'}],
            [{'text': '⏸ Пауза' if enabled else '▶️ Старт', 'callback_data': 'act:toggle'}, {'text': '🏠 Главное меню', 'callback_data': 'menu:main'}],
        ]}

    def broadcast(self, text: str, parse_mode: str | None = 'Markdown') -> None:
        for chat_id in self.enabled_chats():
            try:
                self.send(chat_id, text, reply_markup=self.main_keyboard(), parse_mode=parse_mode)
            except Exception as e:
                log(f'WARN send to {chat_id} failed: {e}')

    def broadcast_document(self, path: Path, caption: str = '') -> None:
        for chat_id in self.enabled_chats():
            try:
                self.send_document(chat_id, path, caption=caption)
            except Exception as e:
                log(f'WARN send document to {chat_id} failed: {e}')

    def setup_commands(self) -> None:
        commands = [
            {'command': 'menu', 'description': 'Главное меню'},
            {'command': 'status', 'description': 'Статус мониторинга'},
            {'command': 'settings', 'description': 'Настройки'},
            {'command': 'groups', 'description': 'Watchlist группы'},
            {'command': 'groupfile', 'description': 'CSV групп'},
            {'command': 'autogroups', 'description': 'Draft авто-групп'},
            {'command': 'watchlist', 'description': 'Получить CSV watchlist'},
            {'command': 'last', 'description': 'Последние сигналы'},
            {'command': 'scan', 'description': 'Скан сейчас'},
            {'command': 'pause', 'description': 'Пауза'},
            {'command': 'resume', 'description': 'Возобновить'},
            {'command': 'whitelist', 'description': 'Whitelist пользователей'},
            {'command': 'allow', 'description': 'Админ: добавить user_id'},
        ]
        self.api('setMyCommands', {'commands': commands}, timeout=20)

    def poll_loop(self, scanner: 'Scanner') -> None:
        log('Telegram polling started')
        while True:
            try:
                res = self.api('getUpdates', {'timeout': 30, 'offset': self.offset}, timeout=40)
                for upd in res.get('result', []):
                    self.offset = max(self.offset, upd.get('update_id', 0) + 1)
                    cb = upd.get('callback_query')
                    if cb:
                        from_user = cb.get('from', {})
                        if not self.is_allowed(int(from_user.get('id', 0))):
                            continue
                        self.handle_callback(cb, scanner)
                        continue
                    msg = upd.get('message') or upd.get('edited_message')
                    if not msg:
                        continue
                    from_user = msg.get('from', {})
                    user_id = int(from_user.get('id', 0))
                    if not self.is_allowed(user_id):
                        # Hard ignore: no reply, no registration.
                        continue
                    chat_id = int(msg['chat']['id'])
                    self.register_chat(chat_id)
                    if msg.get('document'):
                        self.handle_document(chat_id, msg['document'], scanner)
                        continue
                    text = (msg.get('text') or '').strip()
                    if text:
                        self.handle_message(chat_id, text, scanner, user_id)
            except Exception as e:
                log(f'ERROR poll_loop: {e}\n{traceback.format_exc()}')
                time.sleep(5)

    def handle_callback(self, cb: dict[str, Any], scanner: 'Scanner') -> None:
        data = cb.get('data', '')
        msg = cb.get('message') or {}
        chat_id = int(msg.get('chat', {}).get('id'))
        message_id = int(msg.get('message_id'))
        cfg = load_config()
        # Telegram callback queries expire quickly. Answer immediately so button taps
        # do not show an endless loading spinner while we edit/send messages.
        try:
            self.answer_callback(cb.get('id', ''))
        except Exception:
            pass
        try:
            if data == 'menu:main':
                self.edit(chat_id, message_id, main_text(cfg, scanner), self.main_keyboard(cfg))
            elif data == 'menu:status':
                self.edit(chat_id, message_id, status_text(cfg, scanner, len(self.enabled_chats())), self.main_keyboard(cfg))
            elif data == 'menu:health':
                self.edit(chat_id, message_id, health_text(scanner), self.health_keyboard(cfg))
            elif data == 'menu:groups':
                self.edit(chat_id, message_id, groups_text(cfg, scanner), self.groups_keyboard(cfg))
            elif data.startswith('health:'):
                hours = int(data.split(':', 1)[1])
                self.edit(chat_id, message_id, health_text(scanner, hours=hours), self.health_keyboard(cfg))
            elif data == 'menu:settings':
                self.edit(chat_id, message_id, settings_text(cfg), self.settings_keyboard(cfg))
            elif data.startswith('settings:'):
                section = data.split(':', 1)[1]
                self.edit(chat_id, message_id, settings_text(cfg, section), self.settings_keyboard(cfg, section))
            elif data == 'menu:guide_main':
                self.edit(chat_id, message_id, guide_text(), self.main_keyboard(cfg))
            elif data == 'menu:guide':
                self.edit(chat_id, message_id, guide_text(), self.settings_keyboard(cfg))
            elif data == 'menu:last':
                self.answer_callback(cb['id'], 'Отправляю последние сигналы...')
                self.send(chat_id, last_alerts_text(self.con, 10), reply_markup=self.main_keyboard(cfg), parse_mode=None)
                return
            elif data == 'menu:help':
                self.edit(chat_id, message_id, help_text(), self.main_keyboard(cfg))
            elif data == 'act:watchfile':
                self.answer_callback(cb['id'], 'Готовлю файл...')
                path = export_watchlist_files(load_config())
                self.send_document(chat_id, path, caption='Сортированный watchlist. Можно отредактировать и отправить файл обратно боту.')
                return
            elif data == 'act:groupfile':
                self.answer_callback(cb['id'], 'Готовлю groups CSV...')
                path = export_grouped_watchlist_file(load_config())
                self.send_document(chat_id, path, caption='CSV групп: group, enabled, interval_seconds, market_hash_name. Отредактируй group/interval/enabled и отправь файл обратно боту.')
                return
            elif data == 'act:autogroupdraft':
                self.answer_callback(cb['id'], 'Готовлю auto-group draft...')
                path = export_auto_grouping_draft(load_config(), self.con)
                self.send_document(chat_id, path, caption='Draft авто-разбиения без новых Steam-запросов: price-based по уже увиденным listing prices. Можно проверить/отредактировать и отправить обратно боту.')
                return
            elif data == 'act:applyautogroups':
                counts = apply_auto_grouping(load_config(), self.con)
                scanner.clear_group_cooldown()
                msg = '✅ Auto groups применены. Cooldowns очищены, чтобы scheduler пересчитал группы.\n' + '\n'.join(f'- {k}: {v}' for k, v in counts.items())
                self.edit(chat_id, message_id, msg + '\n\n' + groups_text(load_config(), scanner), self.groups_keyboard(load_config()))
                return
            elif data == 'act:defaultgroups':
                cfg = load_config(); ensure_default_groups(cfg); save_config(cfg)
                self.edit(chat_id, message_id, groups_text(load_config(), scanner), self.groups_keyboard(load_config()))
                return
            elif data == 'act:toggle':
                cfg['enabled'] = not bool(cfg.get('enabled', True)); save_config(cfg)
                self.edit(chat_id, message_id, main_text(cfg, scanner), self.main_keyboard(cfg))
            elif data == 'act:scan':
                ok = scanner.start_manual_scan(chat_id)
                if ok:
                    self.edit(chat_id, message_id, '🔍 Ручной скан запущен в фоне. Бот остаётся доступным; результат придёт отдельным сообщением.', self.main_keyboard(cfg))
                else:
                    self.edit(chat_id, message_id, '⏳ Скан уже идёт. Дождись результата — параллельно второй запуск не делаю, чтобы не забить Steam/CSFloat.', self.main_keyboard(cfg))
                return
            elif data.startswith('group:'):
                parts = data.split(':')
                action = parts[1]
                group = parts[2]
                cfg = load_config()
                groups = watchlist_groups(cfg)
                if group not in groups:
                    self.edit(chat_id, message_id, 'Группа не найдена.', self.groups_keyboard(cfg)); return
                if action == 'toggle':
                    groups[group]['enabled'] = not bool(groups[group].get('enabled', True)); save_config(cfg)
                    self.edit(chat_id, message_id, groups_text(load_config(), scanner), self.groups_keyboard(load_config())); return
                if action == 'interval' and len(parts) >= 4:
                    groups[group]['interval_seconds'] = int(parts[3]); save_config(cfg)
                    self.edit(chat_id, message_id, groups_text(load_config(), scanner), self.groups_keyboard(load_config())); return
                if action == 'chunk' and len(parts) >= 4:
                    val = max(1, int(parts[3])); groups[group]['chunk_size'] = val; save_config(cfg)
                    scanner.set_group_cursor(group, 0)
                    self.edit(chat_id, message_id, groups_text(load_config(), scanner), self.groups_keyboard(load_config())); return
                if action == 'noop':
                    self.edit(chat_id, message_id, groups_text(load_config(), scanner), self.groups_keyboard(load_config())); return
                if action == 'clearcd':
                    scanner.clear_group_cooldown(group)
                    self.edit(chat_id, message_id, groups_text(load_config(), scanner), self.groups_keyboard(load_config())); return
                if action == 'scan':
                    ok = scanner.start_manual_scan(chat_id, group=group)
                    self.edit(chat_id, message_id, f"{'🔍 Скан группы запущен' if ok else '⏳ Уже идёт другой scan'}: {group}", self.groups_keyboard(cfg)); return
            elif data.startswith('tier:'):
                _, action, raw = data.split(':', 2)
                if action == 'preset':
                    cfg['allowed_tiers'] = [x for x in raw.split(',') if x]
                elif action == 'toggle':
                    tiers = {str(x).upper() for x in cfg.get('allowed_tiers', ['S','A','B'])}
                    t = raw.upper()
                    if t in tiers:
                        tiers.remove(t)
                    else:
                        tiers.add(t)
                    if not tiers:
                        tiers = {'S'}
                    cfg['allowed_tiers'] = sorted(tiers, key=lambda x: ['S','A','B','C'].index(x) if x in ['S','A','B','C'] else 99)
                save_config(cfg)
                self.edit(chat_id, message_id, settings_text(load_config(), 'tier'), self.settings_keyboard(load_config(), 'tier'))
            elif data.startswith('set:'):
                _, key, raw = data.split(':', 2)
                caster = ALLOWED_SET_KEYS.get(key)
                if caster:
                    cfg[key] = parse_bool(raw) if caster is bool else caster(raw); save_config(cfg)
                section_by_key = {
                    'min_price_usd': 'price', 'max_price_usd': 'price',
                    'min_resale_roi_pct': 'edge', 'min_float_premium_pct': 'edge', 'max_steam_overpay_pct': 'edge',
                    'min_beautiful_score': 'float', 'low_float_threshold': 'float',
                    'interval_seconds': 'time', 'steam_workers': 'time', 'steam_timeout_seconds': 'time', 'alert_send_delay_seconds': 'time', 'csfloat_cache_ttl_seconds': 'time',
                    'auto_safe_mode_enabled': 'time', 'safe_mode_workers': 'time', 'safe_mode_interval_seconds': 'time', 'safe_mode_duration_seconds': 'time', 'safe_mode_empty_rate_pct': 'time',
                    'rate_limit_controller_enabled': 'time', 'rate_limit_cooldown_seconds': 'time', 'rate_limit_cooldown_max_seconds': 'time', 'rate_limit_cooldown_multiplier': 'time', 'rate_limit_empty_rate_pct': 'time', 'rate_limit_safe_workers': 'time',
                }
                section = section_by_key.get(key, 'main')
                self.edit(chat_id, message_id, settings_text(load_config(), section), self.settings_keyboard(load_config(), section))
            # callback already answered at the start of this handler
        except Exception as e:
            log(f'ERROR callback {data}: {e}\n{traceback.format_exc()}')
            try:
                self.answer_callback(cb.get('id', ''), 'Ошибка')
            except Exception:
                pass

    def handle_message(self, chat_id: int, text: str, scanner: 'Scanner', user_id: int) -> None:
        cfg = load_config()
        cmd, _, arg = text.partition(' ')
        cmd = cmd.split('@', 1)[0].lower()
        arg = arg.strip()

        if cmd in ('/start', '/help', '/menu'):
            self.send(chat_id, main_text(cfg, scanner), reply_markup=self.main_keyboard(cfg))
        elif cmd == '/status':
            self.send(chat_id, status_text(cfg, scanner, len(self.enabled_chats())), reply_markup=self.main_keyboard(cfg))
        elif cmd == '/groups':
            self.send(chat_id, groups_text(cfg, scanner), reply_markup=self.groups_keyboard(cfg))
        elif cmd == '/settings':
            self.send(chat_id, settings_text(cfg), reply_markup=self.settings_keyboard(cfg))
        elif cmd in ('/watchlist', '/export_watchlist'):
            path = export_watchlist_files(cfg)
            self.send_document(chat_id, path, caption='Сортированный watchlist. Удали/добавь строки и отправь CSV/TXT обратно боту.')
        elif cmd in ('/export_groups', '/groupfile'):
            path = export_grouped_watchlist_file(cfg)
            self.send_document(chat_id, path, caption='CSV групп: group, enabled, interval_seconds, market_hash_name. Отредактируй и отправь обратно боту.')
        elif cmd in ('/autogroups', '/auto_groups'):
            path = export_auto_grouping_draft(cfg, self.con)
            self.send_document(chat_id, path, caption='Draft auto-groups. Чтобы применить сразу: /apply_autogroups или кнопка ✅ Apply auto groups в /groups.')
        elif cmd == '/apply_autogroups':
            counts = apply_auto_grouping(cfg, self.con)
            scanner.clear_group_cooldown()
            self.send(chat_id, '✅ Auto groups применены:\n' + '\n'.join(f'- {k}: {v}' for k,v in counts.items()), reply_markup=self.groups_keyboard(load_config()), parse_mode=None)
        elif cmd == '/last':
            self.send(chat_id, last_alerts_text(self.con, 10), reply_markup=self.main_keyboard(cfg), parse_mode=None)
        elif cmd == '/add':
            if not arg:
                self.send(chat_id, 'Формат: /add AK-47 | Slate (Factory New)', reply_markup=self.main_keyboard(cfg), parse_mode=None)
                return
            ok, reason = can_monitor(arg)
            if not ok:
                self.send(chat_id, f'Не добавил: {arg}\nПричина: {reason}', parse_mode=None)
                return
            wl = sorted_watchlist(cfg)
            if arg not in wl:
                wl.append(arg)
            cfg['watchlist'] = sorted(set(wl), key=lambda s: s.lower())
            groups = watchlist_groups(cfg)
            groups.setdefault('core', {'enabled': True, 'interval_seconds': int(cfg.get('interval_seconds', 60)), 'items': []})
            if arg not in groups['core'].get('items', []):
                groups['core'].setdefault('items', []).append(arg)
            save_config(cfg)
            self.send(chat_id, f'Добавил и проверил: {arg}', reply_markup=self.main_keyboard(load_config()), parse_mode=None)
        elif cmd == '/set':
            parts = arg.split(maxsplit=1)
            if len(parts) != 2 or parts[0] not in ALLOWED_SET_KEYS:
                self.send(chat_id, 'Формат: /set interval_seconds 60\nДоступно: ' + ', '.join(ALLOWED_SET_KEYS), reply_markup=self.settings_keyboard(cfg), parse_mode=None)
                return
            key, raw = parts; caster = ALLOWED_SET_KEYS[key]
            try:
                val = parse_bool(raw) if caster is bool else caster(raw)
            except Exception:
                self.send(chat_id, f'Не смог прочитать значение для {key}', reply_markup=self.settings_keyboard(cfg), parse_mode=None); return
            cfg[key] = val; save_config(cfg)
            self.send(chat_id, f'Ок: {key} = {val}', reply_markup=self.settings_keyboard(load_config()), parse_mode=None)
        elif cmd == '/pause':
            cfg['enabled'] = False; save_config(cfg); self.send(chat_id, 'Мониторинг поставлен на паузу.', reply_markup=self.main_keyboard(cfg))
        elif cmd == '/resume':
            cfg['enabled'] = True; save_config(cfg); self.send(chat_id, 'Мониторинг включён.', reply_markup=self.main_keyboard(cfg))
        elif cmd == '/scan':
            ok = scanner.start_manual_scan(chat_id)
            if ok:
                self.send(chat_id, '🔍 Ручной скан запущен в фоне. Результат пришлю отдельным сообщением.', reply_markup=self.main_keyboard(cfg))
            else:
                self.send(chat_id, '⏳ Скан уже идёт. Второй параллельно не запускаю.', reply_markup=self.main_keyboard(cfg))
        elif cmd == '/allow' and user_id == ADMIN_USER_ID:
            if not arg.isdigit():
                self.send(chat_id, 'Формат: /allow 123456789', parse_mode=None); return
            uid = int(arg)
            self.con.execute('INSERT OR IGNORE INTO whitelist(user_id, added_by, added_ts) VALUES (?, ?, ?)', (uid, user_id, int(time.time())))
            self.con.commit(); self.send(chat_id, f'Добавил в whitelist: {uid}', parse_mode=None)
        elif cmd == '/revoke' and user_id == ADMIN_USER_ID:
            if not arg.isdigit():
                self.send(chat_id, 'Формат: /revoke 123456789', parse_mode=None); return
            uid = int(arg)
            if uid == ADMIN_USER_ID:
                self.send(chat_id, 'Админа удалить нельзя.', parse_mode=None); return
            self.con.execute('DELETE FROM whitelist WHERE user_id=?', (uid,)); self.con.commit(); self.send(chat_id, f'Удалил из whitelist: {uid}', parse_mode=None)
        elif cmd == '/whitelist' and user_id == ADMIN_USER_ID:
            rows = self.con.execute('SELECT user_id, added_by, added_ts FROM whitelist ORDER BY user_id').fetchall()
            text = 'Whitelist:\n' + '\n'.join(f'- {u} (by {b}, {time.strftime("%Y-%m-%d", time.localtime(ts))})' for u,b,ts in rows)
            self.send(chat_id, text, parse_mode=None)
        else:
            self.send(chat_id, 'Не понял команду. Нажми кнопки ниже или напиши /help', reply_markup=self.main_keyboard(cfg))

    def handle_document(self, chat_id: int, doc: dict[str, Any], scanner: 'Scanner') -> None:
        name = doc.get('file_name') or 'watchlist.txt'
        if not name.lower().endswith(('.txt', '.csv')):
            self.send(chat_id, 'Пришли watchlist файлом .txt или .csv', parse_mode=None)
            return
        self.send(chat_id, 'Получил файл. Проверяю новые предметы на Steam...', parse_mode=None)
        file_id = doc['file_id']
        info = self.api('getFile', {'file_id': file_id}, timeout=20)
        file_path = info.get('result', {}).get('file_path')
        if not file_path:
            self.send(chat_id, 'Не смог получить файл от Telegram.', parse_mode=None); return
        with urllib.request.urlopen(f'{self.file_base}/{file_path}', timeout=60) as r:
            raw = r.read()
        text = raw.decode('utf-8-sig', errors='replace')
        grouped = parse_grouped_watchlist_upload(text)
        if grouped:
            cfg = load_config()
            old_groups = watchlist_groups(cfg)
            old_union = set(sorted_watchlist(cfg))
            incoming_union = set()
            for g in grouped.values():
                incoming_union.update(g.get('items', []))
            new_names = sorted([x for x in incoming_union if x not in old_union], key=lambda s: s.lower())
            valid_new, invalid = [], []
            for n in new_names[:200]:
                ok, reason = can_monitor(n)
                if ok: valid_new.append(n)
                else: invalid.append((n, reason))
                time.sleep(0.25)
            allowed_new = set(valid_new)
            final_groups = {}
            for group, g in grouped.items():
                old_items = set(old_groups.get(group, {}).get('items', []))
                items = [x for x in g.get('items', []) if x in old_union or x in allowed_new or x in old_items]
                final_groups[group] = {
                    'enabled': bool(g.get('enabled', True)),
                    'interval_seconds': int(g.get('interval_seconds', cfg.get('interval_seconds', 60))),
                    'items': clean_names(items),
                    'description': old_groups.get(group, {}).get('description', ''),
                }
            cfg['watchlist_groups'] = final_groups
            save_config(cfg)
            export_grouped_watchlist_file(load_config())
            lines = ['Groups CSV применён.']
            for group, g in watchlist_groups(load_config()).items():
                old_n = len(old_groups.get(group, {}).get('items', []))
                lines.append(f"- {group}: было {old_n}, стало {len(g.get('items', []))}, interval {g.get('interval_seconds')}s, enabled {g.get('enabled')}")
            lines.append(f'Новых проверено: {len(new_names[:200])}; добавлено новых: {len(valid_new)}; отклонено: {len(invalid)}')
            if len(new_names) > 200:
                lines.append('Внимание: проверил первые 200 новых, остальные новые не добавлял — чтобы не забить Steam.')
            if invalid:
                lines.append('\nПервые отклонённые:\n' + '\n'.join(f'- {n}: {r}' for n, r in invalid[:10]))
            self.send(chat_id, '\n'.join(lines), reply_markup=self.groups_keyboard(load_config()), parse_mode=None)
            return

        names = parse_watchlist_upload(text)
        if not names:
            self.send(chat_id, 'В файле не нашёл market_hash_name.', parse_mode=None); return
        cfg = load_config()
        old = set(sorted_watchlist(cfg))
        incoming = sorted(set(names), key=lambda s: s.lower())
        new_names = [x for x in incoming if x not in old]
        valid_new, invalid = [], []
        for n in new_names:
            ok, reason = can_monitor(n)
            if ok: valid_new.append(n)
            else: invalid.append((n, reason))
            time.sleep(0.35)
        final = sorted((old.intersection(incoming)).union(valid_new), key=lambda s: s.lower())
        groups = watchlist_groups(cfg)
        groups.setdefault('core', {'enabled': True, 'interval_seconds': int(cfg.get('interval_seconds', 60)), 'items': []})
        groups['core']['items'] = final
        cfg['watchlist'] = final; save_config(cfg)
        export_watchlist_files(cfg)
        msg = f'Watchlist обновлён.\nБыло: {len(old)}\nВ файле: {len(incoming)}\nНовых проверено: {len(new_names)}\nДобавлено новых: {len(valid_new)}\nОтклонено: {len(invalid)}\nИтого: {len(final)}'
        if invalid:
            msg += '\n\nПервые отклонённые:\n' + '\n'.join(f'- {n}: {r}' for n,r in invalid[:10])
        self.send(chat_id, msg, reply_markup=self.main_keyboard(load_config()), parse_mode=None)


class Scanner:
    def __init__(self, con: sqlite3.Connection, bot: TelegramBot):
        self.con = con
        self.bot = bot
        self.last_scan_summary = None
        self.last_scan_metrics: dict[str, Any] = {}
        self.scan_history = deque(maxlen=60)
        self.scan_lock = threading.Lock()
        self.manual_scan_running = False
        self.safe_mode_until = 0
        self.safe_mode_reason = ''
        self.group_last_scan: dict[str, float] = {}
        self.group_cooldowns: dict[str, dict[str, Any]] = {}
        self.load_group_cooldowns()
        self.force_scan = threading.Event()
        try:
            self.csfloat_client = CSFloat(load_api_key(), delay=0.15)
        except Exception as e:
            self.csfloat_client = None
            log(f'WARN CSFloat client disabled: {e}')
        self.csfloat_cache: dict[str, dict[str, Any]] = {}
        self.csfloat_analysis_cache: dict[tuple[str, float | None], tuple[float, dict[str, Any]]] = {}

    def is_new(self, r: SteamListing) -> bool:
        exists = self.con.execute('SELECT 1 FROM seen WHERE listingid=?', (r.listingid,)).fetchone()
        if exists:
            return False
        self.con.execute('INSERT OR IGNORE INTO seen VALUES (?,?,?,?,?,?,?,?)', (r.listingid, int(time.time()), r.source_query, r.market_hash_name, r.price_usd, r.float_value, r.beautiful_tier, r.beautiful_score))
        return True

    def store_alerts(self, alerts: list[SteamListing]) -> None:
        now = int(time.time())
        for r in alerts:
            self.con.execute('''INSERT INTO alerts(ts, listingid, source_query, market_hash_name, price_usd, float_value, beautiful_tier, beautiful_score, beautiful_reasons, url)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', (now, r.listingid, r.source_query, r.market_hash_name, r.price_usd, r.float_value, r.beautiful_tier, r.beautiful_score, r.beautiful_reasons, r.url))
        self.con.commit()

    def store_scan_metrics(self, m: dict[str, Any]) -> None:
        self.con.execute('''INSERT INTO scan_metrics(ts, health, watchlist, workers, observed, new_count, candidates, alerts, elapsed, source_json, rejects_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', (
                int(m.get('ts') or time.time()), str(m.get('health') or 'unknown'), int(m.get('watchlist') or 0),
                int(m.get('workers') or 0), int(m.get('observed') or 0), int(m.get('new') or 0),
                int(m.get('candidates') or 0), int(m.get('alerts') or 0), float(m.get('elapsed') or 0),
                json.dumps({'pages': m.get('source') or {}, 'empty_reasons': m.get('empty_reasons') or {}, 'empty_samples': m.get('empty_samples') or []}, ensure_ascii=False),
                json.dumps(m.get('rejects') or {}, ensure_ascii=False),
            ))
        self.con.commit()

    def load_group_cooldowns(self) -> None:
        now = time.time()
        try:
            self.con.execute('DELETE FROM group_cooldowns WHERE until_ts <= ?', (now,))
            rows = self.con.execute('SELECT group_name, until_ts, reason, strikes, duration FROM group_cooldowns WHERE until_ts > ?', (now,)).fetchall()
            self.group_cooldowns = {
                str(group): {'until': float(until), 'reason': reason or 'cooldown', 'strikes': int(strikes or 0), 'duration': int(duration or 0)}
                for group, until, reason, strikes, duration in rows
            }
            self.con.commit()
            if self.group_cooldowns:
                log('Restored group cooldowns: ' + ', '.join(f'{g} {self.group_cooldown_status(g)}' for g in self.group_cooldowns))
        except Exception as e:
            log(f'WARN load_group_cooldowns failed: {type(e).__name__}: {e}')

    def persist_group_cooldown(self, group: str, cd: dict[str, Any]) -> None:
        self.con.execute('''INSERT OR REPLACE INTO group_cooldowns(group_name, until_ts, reason, strikes, duration, updated_ts)
            VALUES (?, ?, ?, ?, ?, ?)''', (
                group, float(cd.get('until') or 0), str(cd.get('reason') or 'cooldown'), int(cd.get('strikes') or 0), int(cd.get('duration') or 0), int(time.time())
            ))
        self.con.commit()

    def clear_group_cooldown(self, group: str | None = None) -> None:
        if group:
            self.group_cooldowns.pop(group, None)
            self.con.execute('DELETE FROM group_cooldowns WHERE group_name=?', (group,))
        else:
            self.group_cooldowns.clear()
            self.con.execute('DELETE FROM group_cooldowns')
        self.con.commit()

    def group_cursor(self, group: str) -> int:
        row = self.con.execute('SELECT cursor FROM group_scan_state WHERE group_name=?', (group,)).fetchone()
        return max(0, int(row[0])) if row else 0

    def set_group_cursor(self, group: str, cursor: int) -> None:
        self.con.execute('''INSERT OR REPLACE INTO group_scan_state(group_name, cursor, updated_ts)
            VALUES (?, ?, ?)''', (group, max(0, int(cursor)), int(time.time())))
        self.con.commit()

    def next_group_chunk(self, group: str, items: list[str], chunk_size: int) -> tuple[list[str], int, int, int]:
        total = len(items)
        if total <= 0:
            return [], 0, 0, 0
        chunk_size = max(1, min(int(chunk_size), total))
        cursor = self.group_cursor(group) % total
        end = min(total, cursor + chunk_size)
        chunk = items[cursor:end]
        next_cursor = 0 if end >= total else end
        self.set_group_cursor(group, next_cursor)
        return chunk, cursor + 1, end, next_cursor

    def local_reject_reason(self, r: SteamListing, cfg: dict[str, Any]) -> str | None:
        allowed = {str(x).upper() for x in cfg.get('allowed_tiers', ['S', 'A', 'B'])}
        tier = (r.beautiful_tier or '').upper()
        if allowed and tier and tier not in allowed:
            return 'tier_disabled'
        if allowed and not tier:
            return 'no_float_or_no_tier'
        min_price = cfg.get('min_price_usd')
        if min_price is not None and r.price_usd is not None and r.price_usd < float(min_price):
            return 'below_min_price'
        max_price = cfg.get('max_price_usd')
        if max_price is not None and r.price_usd is not None and r.price_usd > float(max_price):
            return 'above_max_price'
        if r.beautiful_score is not None and r.beautiful_score >= int(cfg.get('min_beautiful_score', 35)):
            return None
        low = cfg.get('low_float_threshold')
        if low is not None and r.float_value is not None and r.float_value <= float(low):
            return None
        return 'weak_float_score'

    def is_interesting(self, r: SteamListing, cfg: dict[str, Any]) -> bool:
        return self.local_reject_reason(r, cfg) is None

    def csfloat_reject_reason(self, r: SteamListing, cfg: dict[str, Any], steam_median_usd: float | None = None, steam_floor_usd: float | None = None) -> str | None:
        """Return reject reason or None.

        Important strategy:
        - CSFloat nearby comps are the *base* price for the same wear/float area.
        - Beautiful float premium is estimated by csfloat_evaluator via beauty_multiplier.
        - A beautiful Steam lot should not be rejected only because raw nearby comps have weak ROI.
        """
        if not self.csfloat_client or not r.market_hash_name:
            return None
        try:
            ttl = int(cfg.get('csfloat_cache_ttl_seconds', 600))
            bucket = round(float(r.float_value), 3) if r.float_value is not None else None
            cache_key = (r.market_hash_name, bucket)
            cached = self.csfloat_analysis_cache.get(cache_key)
            if cached and time.time() - cached[0] <= ttl:
                ev = dict(cached[1])
            else:
                ev = evaluate_nearby_float(
                    self.csfloat_client,
                    r.market_hash_name,
                    r.float_value,
                    r.price_usd,
                    min_comps=5,
                    limit=20,
                    beautiful_score=r.beautiful_score,
                    beautiful_reasons=r.beautiful_reasons,
                    beautiful_tier=r.beautiful_tier,
                )
                self.csfloat_analysis_cache[cache_key] = (time.time(), dict(ev))

            ev['steam_visible_median_usd'] = steam_median_usd
            ev['steam_visible_floor_usd'] = steam_floor_usd
            ev['buy_price_usd'] = r.price_usd
            if steam_median_usd and r.price_usd:
                ev['steam_delta_to_median_pct'] = round((float(r.price_usd) / float(steam_median_usd) - 1) * 100, 1)
            else:
                ev['steam_delta_to_median_pct'] = None
            self.csfloat_cache[r.listingid] = ev

            roi = ev.get('resale_roi_pct')
            base_roi = ev.get('base_resale_roi_pct')
            prem = ev.get('float_premium_pct')
            steam_base_ok = ev.get('steam_base_price_ok')
            steam_delta = ev.get('steam_delta_to_median_pct')
            beauty_score = r.beautiful_score if r.beautiful_score is not None else ev.get('beautiful_score')
            min_roi = float(cfg.get('min_resale_roi_pct', 10))
            min_prem = float(cfg.get('min_float_premium_pct', 10))
            max_steam_overpay = float(cfg.get('max_steam_overpay_pct', 10))
            min_beauty = int(cfg.get('min_beautiful_score', 35))

            is_strong_beauty = isinstance(beauty_score, (int, float)) and beauty_score >= max(55, min_beauty)
            is_beautiful_candidate = isinstance(beauty_score, (int, float)) and beauty_score >= min_beauty

            # Prefer CSFloat-derived Steam fair range over visible Steam median. Visible Steam page
            # can be noisy and can accidentally reject exactly the hidden-premium lots we need.
            if steam_base_ok is False:
                if is_strong_beauty:
                    # Still alert for manual review if the number is very attractive.
                    return None
                return 'steam_overpay_vs_csfloat_base'

            # Legacy Steam visible median check: only apply to weak beauty candidates.
            if (
                not is_strong_beauty
                and isinstance(steam_delta, (int, float))
                and steam_delta > max_steam_overpay
            ):
                return 'steam_overpay'

            if roi is None:
                return 'no_csfloat_target'

            # For beautiful float hunting, raw/base ROI may be negative because Steam is normally
            # 10-15% above CSFloat. Do not reject if the lot is base-priced and beauty premium
            # creates a plausible resale story.
            if isinstance(roi, (int, float)) and roi < min_roi:
                if is_beautiful_candidate and steam_base_ok is not False:
                    return None
                return 'roi_below_min'

            if isinstance(prem, (int, float)) and prem < min_prem:
                if is_strong_beauty and steam_base_ok is not False:
                    return None
                return 'premium_below_min'

            return None
        except Exception as e:
            log(f'WARN CSFloat filter failed {r.listingid}: {e}')
            return 'csfloat_error_open'

    def passes_csfloat_filter(self, r: SteamListing, cfg: dict[str, Any], steam_median_usd: float | None = None, steam_floor_usd: float | None = None) -> bool:
        return self.csfloat_reject_reason(r, cfg, steam_median_usd, steam_floor_usd) is None

    def safe_mode_active(self) -> bool:
        return time.time() < float(self.safe_mode_until or 0)

    def safe_mode_status(self) -> str:
        if not self.safe_mode_active():
            return 'off'
        mins = max(0, int((self.safe_mode_until - time.time()) // 60))
        return f'on ~{mins}m: {self.safe_mode_reason}'

    def group_cooldown_status(self, group: str) -> str:
        cd = self.group_cooldowns.get(group) or {}
        until = float(cd.get('until') or 0)
        if until <= time.time():
            return 'off'
        mins = max(0, int((until - time.time()) // 60))
        reason = cd.get('reason') or 'cooldown'
        return f'{reason} ~{mins}m'

    def group_in_cooldown(self, group: str) -> bool:
        return self.group_cooldown_status(group) != 'off'

    def update_rate_limit_controller(self, cfg: dict[str, Any], group: str | None, metrics: dict[str, Any]) -> None:
        if not group or not cfg.get('rate_limit_controller_enabled', True):
            return
        src = metrics.get('source') or {}
        reasons = metrics.get('empty_reasons') or {}
        pages = int(src.get('success', 0) or 0) + int(src.get('empty', 0) or 0) + int(src.get('error', 0) or 0)
        if pages <= 0:
            return
        rl = int(reasons.get('rate_limited', 0) or 0)
        rl_rate = rl / pages * 100
        threshold = float(cfg.get('rate_limit_empty_rate_pct', 50))
        if rl_rate < threshold:
            if int(src.get('success', 0) or 0) > 0:
                cd = self.group_cooldowns.get(group)
                if cd:
                    cd['strikes'] = max(0, int(cd.get('strikes', 0)) - 1)
            return
        prev = self.group_cooldowns.get(group) or {}
        strikes = int(prev.get('strikes', 0)) + 1
        base = int(cfg.get('rate_limit_cooldown_seconds', 300))
        mult = float(cfg.get('rate_limit_cooldown_multiplier', 2.0))
        max_cd = int(cfg.get('rate_limit_cooldown_max_seconds', 1800))
        duration = min(max_cd, int(base * (mult ** max(0, strikes - 1))))
        reason = f'rate_limited {rl}/{pages} pages ({rl_rate:.0f}%)'
        self.group_cooldowns[group] = {'until': time.time() + duration, 'reason': reason, 'strikes': strikes, 'duration': duration}
        self.persist_group_cooldown(group, self.group_cooldowns[group])
        log(f'RATE LIMIT COOLDOWN group {group}: {reason}; cooldown {duration}s; strikes {strikes}')

    def maybe_update_safe_mode(self, cfg: dict[str, Any], metrics: dict[str, Any]) -> None:
        if not cfg.get('auto_safe_mode_enabled', True):
            return
        hist = list(self.scan_history)[-int(cfg.get('safe_mode_min_scans', 3)):]
        if len(hist) < int(cfg.get('safe_mode_min_scans', 3)):
            return
        src = Counter()
        for m in hist:
            src.update(m.get('source', {}) or {})
        pages = src.get('success', 0) + src.get('empty', 0) + src.get('error', 0)
        empty_rate = (src.get('empty', 0) / pages * 100) if pages else 0
        timeout_rate = (src.get('timeout', 0) / pages * 100) if pages else 0
        threshold = float(cfg.get('safe_mode_empty_rate_pct', 60))
        if empty_rate >= threshold or timeout_rate >= 15:
            duration = int(cfg.get('safe_mode_duration_seconds', 900))
            self.safe_mode_until = max(float(self.safe_mode_until or 0), time.time() + duration)
            self.safe_mode_reason = f'empty {empty_rate:.0f}%, timeout {timeout_rate:.0f}% за последние {len(hist)} сканов'
            log(f'AUTO SAFE MODE ON: {self.safe_mode_reason}; duration {duration}s')
        elif self.safe_mode_active() and metrics.get('health') == 'ok':
            # Keep current cooldown; one good scan is not enough to disable early.
            pass

    def scan_once(self, cfg: dict[str, Any], watch_override: list[str] | None = None, group_name: str | None = None, chunk_label: str | None = None) -> tuple[str, list[SteamListing]]:
        if not self.scan_lock.acquire(blocking=False):
            summary = 'Скан пропущен: уже идёт другой scan'
            self.last_scan_summary = summary
            return summary, []
        try:
            watch = sorted(clean_names(watch_override), key=lambda s: s.lower()) if watch_override is not None else sorted_watchlist(cfg)
            all_count = 0; new_count = 0; alerts: list[SteamListing] = []
            source = Counter(); rejects = Counter(); empty_reasons = Counter()
            empty_samples: list[str] = []
            candidates = 0
            requested_workers = max(1, min(int(cfg.get('steam_workers', 4)), 12))
            workers = max(1, min(int(cfg.get('safe_mode_workers', 3)), requested_workers)) if self.safe_mode_active() else requested_workers
            timeout = max(5, min(int(cfg.get('steam_timeout_seconds', 12)), 30))
            sample_lock = threading.Lock()

            def fetch_one(name: str) -> tuple[str, list[SteamListing], str | None, dict[str, Any]]:
                try:
                    html, meta = fetch_steam_html_meta(name, timeout=timeout)
                    rows = parse_listings(html, name)
                    if not rows:
                        diag = classify_empty_html(html, meta.get('status'))
                        diag.update({'query': name, 'url': meta.get('url'), 'html_size': meta.get('html_size')})
                        meta['empty_diag'] = diag
                        with sample_lock:
                            if len(empty_samples) < 3:
                                sample_path = save_empty_sample(name, html, diag)
                                if sample_path:
                                    meta['sample_path'] = sample_path
                                    empty_samples.append(sample_path)
                    return name, rows, None, meta
                except Exception as e:
                    return name, [], f'{type(e).__name__}: {e}', {}

            start = time.time()
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                future_map = {pool.submit(fetch_one, name): name for name in watch}
                for fut in concurrent.futures.as_completed(future_map):
                    name, rows, err, meta = fut.result()
                    if err:
                        source['error'] += 1
                        if 'TimeoutError' in err:
                            source['timeout'] += 1
                        log(f'ERROR scan {name}: {err}')
                        continue
                    if rows:
                        source['success'] += 1
                    else:
                        source['empty'] += 1
                        diag = (meta or {}).get('empty_diag') or {}
                        reason = diag.get('reason') or 'unknown_empty'
                        empty_reasons[reason] += 1
                    steam_prices = sorted([float(x.price_usd) for x in rows if x.price_usd is not None])
                    steam_median = float(statistics.median(steam_prices)) if steam_prices else None
                    steam_floor = steam_prices[0] if steam_prices else None
                    all_count += len(rows)
                    for r in rows:
                        is_new = self.is_new(r)
                        if is_new: new_count += 1
                        if not (is_new or not cfg.get('alert_only_new', True)):
                            rejects['already_seen'] += 1
                            continue
                        local_reason = self.local_reject_reason(r, cfg)
                        if local_reason:
                            rejects[local_reason] += 1
                            continue
                        candidates += 1
                        cs_reason = self.csfloat_reject_reason(r, cfg, steam_median, steam_floor)
                        if cs_reason:
                            rejects[cs_reason] += 1
                            continue
                        alerts.append(r)
            self.con.commit()
            if alerts: self.store_alerts(alerts)
            elapsed = time.time() - start
            health = 'ok' if source['success'] else ('degraded' if source['error'] or source['timeout'] else 'empty')
            metrics = {
                'ts': int(time.time()), 'watchlist': len(watch), 'group': group_name or 'all', 'workers': workers, 'requested_workers': requested_workers, 'observed': all_count,
                'new': new_count, 'alerts': len(alerts), 'elapsed': round(elapsed, 1), 'candidates': candidates,
                'source': dict(source), 'rejects': dict(rejects), 'empty_reasons': dict(empty_reasons), 'empty_samples': list(empty_samples), 'health': health, 'safe_mode': self.safe_mode_active(),
            }
            self.last_scan_metrics = metrics
            self.scan_history.append(metrics)
            self.maybe_update_safe_mode(cfg, metrics)
            self.update_rate_limit_controller(cfg, group_name, metrics)
            metrics['safe_mode'] = self.safe_mode_active()
            metrics['safe_mode_status'] = self.safe_mode_status()
            self.last_scan_metrics = metrics
            self.store_scan_metrics(metrics)
            safe_suffix = f", safe {self.safe_mode_status()}" if self.safe_mode_active() else ''
            cd_status = self.group_cooldown_status(group_name) if group_name else 'off'
            cd_suffix = f", cooldown {cd_status}" if cd_status != 'off' else ''
            label = f"group {group_name}" if group_name else 'watchlist'
            if chunk_label:
                label += f" {chunk_label}"
            empty_suffix = ''
            if empty_reasons:
                empty_suffix = ', empty ' + ','.join(f'{k}:{v}' for k, v in empty_reasons.most_common(2))
            summary = f"Скан: {label} {len(watch)}, workers {workers}/{requested_workers}, observed {all_count}, new {new_count}, candidates {candidates}, alerts {len(alerts)}, health {health}, elapsed {elapsed:.1f}s{safe_suffix}{cd_suffix}{empty_suffix}"
            self.last_scan_summary = summary
            return summary, alerts
        finally:
            self.scan_lock.release()

    def start_manual_scan(self, chat_id: int, group: str | None = None) -> bool:
        if self.manual_scan_running or self.scan_lock.locked():
            return False
        self.manual_scan_running = True

        def worker() -> None:
            try:
                label = f'группы {group}' if group else 'всего watchlist'
                self.bot.send(chat_id, f'🔍 Ручной скан {label} поставлен в очередь. Я пришлю результат отдельным сообщением.', reply_markup=self.bot.main_keyboard(load_config()))
                cfg = load_config()
                watch = group_items(cfg, group) if group else None
                summary, alerts = self.scan_once(cfg, watch_override=watch, group_name=group)
                self.bot.send(chat_id, summary + '\n\n' + health_summary(self), reply_markup=self.bot.main_keyboard(load_config()))
                delay = float(load_config().get('alert_send_delay_seconds', 2.0))
                for a in alerts[:20]:
                    self.bot.send(chat_id, plain_alert(a, self.csfloat_client, self.csfloat_cache.get(a.listingid)), reply_markup=self.bot.main_keyboard(load_config()), parse_mode=None)
                    time.sleep(delay)
            except Exception as e:
                log(f'ERROR manual scan: {e}\n{traceback.format_exc()}')
                try:
                    self.bot.send(chat_id, f'Ручной скан упал: {type(e).__name__}', parse_mode=None)
                except Exception:
                    pass
            finally:
                self.manual_scan_running = False

        threading.Thread(target=worker, daemon=True).start()
        return True

    def loop(self) -> None:
        log('Scanner loop started')
        while True:
            try:
                start_ts = time.time(); cfg = load_config(); interval = int(cfg.get('safe_mode_interval_seconds', 120)) if self.safe_mode_active() else int(cfg.get('interval_seconds', 60))
                if cfg.get('enabled', True):
                    scanned_any = False
                    scanned_count = 0
                    max_groups = max(1, int(cfg.get('max_groups_per_cycle', 1)))
                    for group, g in watchlist_groups(cfg).items():
                        if not bool(g.get('enabled', True)):
                            continue
                        items = group_items(cfg, group)
                        if not items:
                            continue
                        if self.group_in_cooldown(group):
                            continue
                        group_interval = int(g.get('interval_seconds') or interval)
                        due_after = int(cfg.get('safe_mode_interval_seconds', 120)) if self.safe_mode_active() else group_interval
                        if time.time() - self.group_last_scan.get(group, 0) < due_after:
                            continue
                        chunk_size = int(g.get('chunk_size') or cfg.get('default_group_chunk_size', 20))
                        chunk, chunk_start, chunk_end, next_cursor = self.next_group_chunk(group, items, chunk_size)
                        if not chunk:
                            continue
                        chunk_label = f"chunk {chunk_start}-{chunk_end}/{len(items)}"
                        summary, alerts = self.scan_once(cfg, watch_override=chunk, group_name=group, chunk_label=chunk_label)
                        self.group_last_scan[group] = time.time()
                        scanned_any = True
                        log(summary)
                        if alerts:
                            delay = float(cfg.get('alert_send_delay_seconds', 2.0))
                            for r in alerts[:20]:
                                self.bot.broadcast(plain_alert(r, self.csfloat_client, self.csfloat_cache.get(r.listingid)), parse_mode=None)
                                time.sleep(delay)
                        scanned_count += 1
                        if scanned_count >= max_groups:
                            break
                    if not scanned_any:
                        log('Scanner: no group due')
                else:
                    log('Scanner paused')
                elapsed = time.time() - start_ts
                self.force_scan.wait(timeout=max(5, interval - elapsed)); self.force_scan.clear()
            except Exception as e:
                log(f'ERROR scanner loop: {e}\n{traceback.format_exc()}')
                time.sleep(10)


def can_monitor(name: str) -> tuple[bool, str]:
    name = name.strip()
    if not name or '|' not in name:
        return False, 'похоже не на market_hash_name оружейного скина'
    try:
        rows = parse_listings(fetch_html(name), name)
        if rows:
            return True, f'ok, visible listings {len(rows)}'
        return False, 'Steam открыл страницу, но видимых лотов не найдено'
    except Exception as e:
        return False, f'ошибка проверки Steam: {type(e).__name__}'


def parse_watchlist_upload(text: str) -> list[str]:
    names: list[str] = []
    sample = text[:4096]
    if ',' in sample or ';' in sample or '\t' in sample:
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=',;\t')
            reader = csv.DictReader(io.StringIO(text), dialect=dialect)
            if reader.fieldnames:
                col = None
                for c in reader.fieldnames:
                    if c and c.strip().lower() in ('market_hash_name', 'name', 'skin', 'item'):
                        col = c; break
                if col:
                    for row in reader:
                        val = (row.get(col) or '').strip()
                        if val and not val.startswith('#'):
                            names.append(val)
                    return clean_names(names)
        except Exception:
            pass
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if ',' in line:
            line = line.split(',', 1)[0].strip()
        names.append(line)
    return clean_names(names)


def parse_grouped_watchlist_upload(text: str) -> dict[str, dict[str, Any]] | None:
    sample = text[:4096]
    if not (',' in sample or ';' in sample or '\t' in sample):
        return None
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=',;\t')
        reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    except Exception:
        return None
    if not reader.fieldnames:
        return None
    fields = {c.strip().lower(): c for c in reader.fieldnames if c}
    group_col = fields.get('group')
    name_col = fields.get('market_hash_name') or fields.get('name') or fields.get('skin') or fields.get('item')
    if not group_col or not name_col:
        return None
    enabled_col = fields.get('enabled')
    interval_col = fields.get('interval_seconds') or fields.get('interval')
    chunk_col = fields.get('chunk_size') or fields.get('chunk')
    groups: dict[str, dict[str, Any]] = {}
    for row in reader:
        group = re.sub(r'[^a-zA-Z0-9_-]+', '_', (row.get(group_col) or 'core').strip().lower()).strip('_') or 'core'
        name = (row.get(name_col) or '').strip()
        if not name or name.startswith('#'):
            continue
        g = groups.setdefault(group, {'enabled': True, 'interval_seconds': 60, 'items': []})
        if enabled_col and row.get(enabled_col):
            g['enabled'] = parse_bool(str(row.get(enabled_col)))
        if interval_col and row.get(interval_col):
            try:
                g['interval_seconds'] = int(float(str(row.get(interval_col))))
            except Exception:
                pass
        if chunk_col and row.get(chunk_col):
            try:
                g['chunk_size'] = max(1, int(float(str(row.get(chunk_col)))))
            except Exception:
                pass
        g['items'].append(name)
    for g in groups.values():
        g['items'] = clean_names(g['items'])
    return groups or None


def clean_names(names: list[str]) -> list[str]:
    out = []
    seen = set()
    for n in names:
        n = re.sub(r'\s+', ' ', n).strip().strip('"')
        if n and n not in seen:
            seen.add(n); out.append(n)
    return out


def export_watchlist_files(cfg: dict[str, Any]) -> Path:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    wl = sorted_watchlist(cfg)
    csv_path = EXPORT_DIR / 'steam_watchlist_sorted.csv'
    with csv_path.open('w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['market_hash_name'])
        for name in wl:
            w.writerow([name])
    txt_path = EXPORT_DIR / 'steam_watchlist_sorted.txt'
    txt_path.write_text('\n'.join(wl) + '\n', encoding='utf-8')
    export_grouped_watchlist_file(cfg)
    return csv_path


def export_grouped_watchlist_file(cfg: dict[str, Any]) -> Path:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = EXPORT_DIR / 'steam_watchlist_groups.csv'
    with path.open('w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['group', 'enabled', 'interval_seconds', 'chunk_size', 'market_hash_name'])
        for group, g in watchlist_groups(cfg).items():
            for name in group_items(cfg, group):
                w.writerow([group, bool(g.get('enabled', True)), int(g.get('interval_seconds', cfg.get('interval_seconds', 60))), int(g.get('chunk_size', cfg.get('default_group_chunk_size', 20))), name])
    return path


def ensure_default_groups(cfg: dict[str, Any]) -> None:
    groups = watchlist_groups(cfg)
    defaults = {
        'core': (True, 60, 6, 'ключевые ликвидные предметы'),
        'cheap': (True, 300, 20, 'дешёвые/шумные предметы, реже'),
        'premium': (True, 180, 10, 'дорогие предметы, аккуратный мониторинг'),
        'test': (True, 900, 10, 'ручные эксперименты, очень редко'),
    }
    existing_items = set(sorted_watchlist(cfg))
    for name, (enabled, interval, chunk, desc) in defaults.items():
        groups.setdefault(name, {'enabled': enabled, 'interval_seconds': interval, 'chunk_size': chunk, 'items': [], 'description': desc})
        groups[name].setdefault('chunk_size', chunk)
    if not groups['core'].get('items') and existing_items:
        groups['core']['items'] = sorted(existing_items, key=lambda s: s.lower())


def seen_price_stats(con: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = con.execute('''SELECT market_hash_name, price_usd FROM seen
                          WHERE market_hash_name IS NOT NULL AND price_usd IS NOT NULL AND price_usd > 0''').fetchall()
    vals: dict[str, list[float]] = defaultdict(list)
    for name, price in rows:
        vals[str(name)].append(float(price))
    out: dict[str, dict[str, Any]] = {}
    for name, prices in vals.items():
        prices = sorted(prices)
        out[name] = {'seen_count': len(prices), 'median_price': round(float(statistics.median(prices)), 2), 'min_price': round(float(prices[0]), 2)}
    return out


def auto_group_for_item(name: str, stats: dict[str, Any]) -> tuple[str, str]:
    median_price = stats.get('median_price')
    seen_count = int(stats.get('seen_count') or 0)
    if median_price is None:
        return 'test', 'no_seen_price_yet'
    price = float(median_price)
    if price < 3:
        return 'cheap', f'median ${price:.2f} < $3'
    if price > 50:
        return 'premium', f'median ${price:.2f} > $50'
    if seen_count < 5:
        return 'test', f'low_seen_count {seen_count}'
    return 'core', f'median ${price:.2f}, seen {seen_count}'


def build_auto_grouping(cfg: dict[str, Any], con: sqlite3.Connection) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    ensure_default_groups(cfg)
    stats_by_name = seen_price_stats(con)
    groups = {
        'core': {'enabled': True, 'interval_seconds': 60, 'chunk_size': 6, 'items': [], 'description': 'auto: $3–50, достаточно наблюдений'},
        'cheap': {'enabled': True, 'interval_seconds': 300, 'chunk_size': 20, 'items': [], 'description': 'auto: медианная Steam цена < $3, реже'},
        'premium': {'enabled': True, 'interval_seconds': 180, 'chunk_size': 10, 'items': [], 'description': 'auto: медианная Steam цена > $50, аккуратнее'},
        'test': {'enabled': True, 'interval_seconds': 900, 'chunk_size': 10, 'items': [], 'description': 'auto: нет цены или мало наблюдений, очень редко'},
    }
    rows: list[dict[str, Any]] = []
    for name in sorted_watchlist(cfg):
        st = stats_by_name.get(name, {})
        group, reason = auto_group_for_item(name, st)
        groups[group]['items'].append(name)
        rows.append({'group': group, 'enabled': groups[group]['enabled'], 'interval_seconds': groups[group]['interval_seconds'], 'chunk_size': groups[group]['chunk_size'], 'market_hash_name': name, 'median_price': st.get('median_price', ''), 'seen_count': st.get('seen_count', 0), 'reason': reason})
    for g in groups.values():
        g['items'] = sorted(clean_names(g['items']), key=lambda s: s.lower())
    return groups, rows


def export_auto_grouping_draft(cfg: dict[str, Any], con: sqlite3.Connection) -> Path:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    _, rows = build_auto_grouping(cfg, con)
    path = EXPORT_DIR / 'steam_watchlist_auto_groups_draft.csv'
    with path.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=['group', 'enabled', 'interval_seconds', 'chunk_size', 'market_hash_name', 'median_price', 'seen_count', 'reason'])
        w.writeheader(); w.writerows(rows)
    return path


def apply_auto_grouping(cfg: dict[str, Any], con: sqlite3.Connection) -> dict[str, int]:
    groups, _ = build_auto_grouping(cfg, con)
    cfg['watchlist_groups'] = groups
    cfg['watchlist'] = sorted_watchlist(cfg)
    save_config(cfg)
    con.execute('DELETE FROM group_scan_state')
    con.commit()
    export_grouped_watchlist_file(cfg)
    return {k: len(v.get('items', [])) for k, v in groups.items()}


def groups_text(cfg: dict[str, Any], scanner: Scanner) -> str:
    lines = ['**📁 Watchlist groups**', 'Группы позволяют сканировать важные предметы чаще, а шумные — реже.']
    total = 0
    for name, g in watchlist_groups(cfg).items():
        items = group_items(cfg, name)
        total += len(items)
        enabled = 'ON' if g.get('enabled', True) else 'OFF'
        last = scanner.group_last_scan.get(name)
        last_txt = time.strftime('%H:%M:%S', time.localtime(last)) if last else 'ещё не сканировали'
        cd = scanner.group_cooldown_status(name)
        cd_txt = f' · cooldown `{cd}`' if cd != 'off' else ''
        chunk_size = int(g.get('chunk_size', cfg.get('default_group_chunk_size', 20)))
        cursor = scanner.group_cursor(name) if items else 0
        chunk_txt = f' · chunk `{min(chunk_size, len(items))}/{len(items)}` next `{cursor + 1 if items else 0}`'
        lines.append(f"\n- `{name}` · {enabled} · `{len(items)}` items · every `{g.get('interval_seconds', cfg.get('interval_seconds', 60))}s`{chunk_txt} · last `{last_txt}`{cd_txt}")
        if g.get('description'):
            lines.append(f"  {g.get('description')}")
    lines.append(f"\nИтого уникальных items: `{len(sorted_watchlist(cfg))}`. Следующий продуктовый шаг — загрузка CSV сразу в выбранную группу.")
    return '\n'.join(lines)


def main_text(cfg: dict[str, Any], scanner: Scanner) -> str:
    state = '🟢 включён' if cfg.get('enabled', True) else '⏸ на паузе'
    last = scanner.last_scan_summary or 'ещё не было скана после запуска'
    return (
        '**CS2 Steam Float Watcher**\n'
        'Покупок не делает — только мониторит Steam TP и шлёт кандидатов на ручную проверку.\n\n'
        f'Статус: {state}\n'
        f'Период: `{cfg.get("interval_seconds")}s`\n'
        f'Watchlist: `{len(sorted_watchlist(cfg))}` предметов\n'
        f'Последний скан: {last}\n\n'
        'Watchlist теперь редактируется через CSV/TXT файл: нажми 📄 Watchlist файл, отредактируй и отправь обратно.'
    )


def reason_label(key: str) -> str:
    return {
        'already_seen': 'уже видели раньше',
        'tier_disabled': 'tier выключен',
        'no_float_or_no_tier': 'нет float/tier',
        'below_min_price': 'ниже min price',
        'above_max_price': 'выше max price',
        'weak_float_score': 'слабый float score',
        'steam_overpay': 'Steam overpay выше лимита',
        'no_csfloat_target': 'нет CSFloat target',
        'roi_below_min': 'ROI ниже минимума',
        'premium_below_min': 'premium ниже минимума',
        'csfloat_error_open': 'ошибка CSFloat, пропуск открыт',
    }.get(key, key)


def health_summary(scanner: Scanner) -> str:
    m = getattr(scanner, 'last_scan_metrics', {}) or {}
    if not m:
        return '🩺 Health: ещё нет метрик после запуска.'
    src = m.get('source', {}) or {}
    rejects = m.get('rejects', {}) or {}
    empty_reasons = m.get('empty_reasons', {}) or {}
    empty_top = ', '.join(f'{k}: {v}' for k, v in sorted(empty_reasons.items(), key=lambda x: x[1], reverse=True)[:3]) or 'нет данных'
    samples = m.get('empty_samples') or []
    top = ', '.join(f'{reason_label(k)}: {v}' for k, v in sorted(rejects.items(), key=lambda x: x[1], reverse=True)[:5]) or 'нет данных'
    return (
        f'🩺 Health: `{m.get("health", "unknown")}`\n'
        f'- Steam pages: success `{src.get("success",0)}`, empty `{src.get("empty",0)}`, errors `{src.get("error",0)}`, timeouts `{src.get("timeout",0)}`\n'
        f'- observed/new/candidates/alerts: `{m.get("observed",0)}` / `{m.get("new",0)}` / `{m.get("candidates",0)}` / `{m.get("alerts",0)}`\n'
        f'- elapsed: `{m.get("elapsed", "—")}s`\n'
        f'- empty reasons: {empty_top}\n'
        f'- empty samples: `{len(samples)}` saved\n'
        f'- top reject: {top}'
    )


def health_recommendation(src_total: Counter, rej_total: Counter, scans: int, avg_elapsed: float | str) -> str:
    pages = src_total.get('success', 0) + src_total.get('empty', 0) + src_total.get('error', 0)
    empty_rate = (src_total.get('empty', 0) / pages * 100) if pages else 0
    timeout_rate = (src_total.get('timeout', 0) / pages * 100) if pages else 0
    if empty_rate > 60 or timeout_rate > 15:
        return 'Рекомендация: Steam source нестабилен — попробуй workers 3 и timeout 12–20s; proxy/VPN пока не трогаем без изоляции.'
    if rej_total.get('weak_float_score', 0) > max(20, rej_total.get('steam_overpay', 0) * 2):
        return 'Рекомендация: если хочешь больше тестовых сигналов — снизь score или включи больше tiers; если качество важнее — оставь как есть.'
    if rej_total.get('steam_overpay', 0) > 20:
        return 'Рекомендация: много лотов уже дороже обычной Steam цены. Для теста можно поднять overpay до 10–25%, для качества — оставить 5%.'
    if rej_total.get('roi_below_min', 0) + rej_total.get('premium_below_min', 0) > 20:
        return 'Рекомендация: CSFloat не подтверждает premium/ROI. Проверь, не слишком ли строгие ROI/premium или не шумный ли watchlist.'
    return 'Рекомендация: критичных bottleneck по последнему окну не видно. Следующий шаг — watchlist groups.'


def load_metric_rows(con: sqlite3.Connection, hours: int) -> list[dict[str, Any]]:
    since = int(time.time()) - hours * 3600
    rows = con.execute('''SELECT ts, health, watchlist, workers, observed, new_count, candidates, alerts, elapsed, source_json, rejects_json
                          FROM scan_metrics WHERE ts >= ? ORDER BY ts ASC''', (since,)).fetchall()
    out = []
    for ts, health, watchlist, workers, observed, new_count, candidates, alerts, elapsed, source_json, rejects_json in rows:
        src_raw = json.loads(source_json or '{}')
        if isinstance(src_raw, dict) and 'pages' in src_raw:
            source = src_raw.get('pages') or {}
            empty_reasons = src_raw.get('empty_reasons') or {}
            empty_samples = src_raw.get('empty_samples') or []
        else:
            source = src_raw if isinstance(src_raw, dict) else {}
            empty_reasons = {}
            empty_samples = []
        out.append({'ts': ts, 'health': health, 'watchlist': watchlist, 'workers': workers, 'observed': observed,
                    'new': new_count, 'candidates': candidates, 'alerts': alerts, 'elapsed': elapsed,
                    'source': source, 'empty_reasons': empty_reasons, 'empty_samples': empty_samples, 'rejects': json.loads(rejects_json or '{}')})
    return out


def health_text(scanner: Scanner, hours: int | None = None) -> str:
    hist = load_metric_rows(scanner.con, hours) if hours else list(getattr(scanner, 'scan_history', []))
    if not hist:
        return '🩺 Health / почему 0\n\nПока нет сканов после запуска.'
    src_total = Counter()
    empty_reason_total = Counter()
    sample_paths: list[str] = []
    rej_total = Counter()
    alerts = observed = new = candidates = 0
    elapsed = []
    for m in hist:
        src_total.update(m.get('source', {}) or {})
        empty_reason_total.update(m.get('empty_reasons', {}) or {})
        for p in (m.get('empty_samples') or []):
            if p not in sample_paths and len(sample_paths) < 5:
                sample_paths.append(p)
        rej_total.update(m.get('rejects', {}) or {})
        alerts += int(m.get('alerts', 0) or 0)
        observed += int(m.get('observed', 0) or 0)
        new += int(m.get('new', 0) or 0)
        candidates += int(m.get('candidates', 0) or 0)
        if m.get('elapsed') is not None:
            elapsed.append(float(m.get('elapsed')))
    top_rej = '\n'.join(f'- {reason_label(k)}: `{v}`' for k, v in sorted(rej_total.items(), key=lambda x: x[1], reverse=True)[:8]) or '- нет отсева'
    top_empty = '\n'.join(f'- {k}: `{v}`' for k, v in sorted(empty_reason_total.items(), key=lambda x: x[1], reverse=True)[:6]) or '- нет empty diagnostics'
    sample_txt = '\n'.join(f'- `{p}`' for p in sample_paths[:3]) or '- нет сохранённых samples'
    avg_elapsed = round(sum(elapsed)/len(elapsed), 1) if elapsed else '—'
    window = f'последние {len(hist)} сканов' if hours is None else f'последние {hours}ч ({len(hist)} сканов)'
    rec = health_recommendation(src_total, rej_total, len(hist), avg_elapsed)
    return (
        '**🩺 Health / почему нет алертов**\n'
        f'Окно: `{window}`.\n\n'
        '**Steam source:**\n'
        f'- success pages: `{src_total.get("success",0)}`\n'
        f'- empty pages: `{src_total.get("empty",0)}`\n'
        f'- errors: `{src_total.get("error",0)}`; timeouts: `{src_total.get("timeout",0)}`\n'
        f'- avg elapsed: `{avg_elapsed}s`\n'
        f'- auto-safe status: `{scanner.safe_mode_status()}`\n\n'
        '**Empty diagnostics:**\n'
        f'{top_empty}\n'
        f'**Samples:**\n{sample_txt}\n\n'
        '**Pipeline:**\n'
        f'- observed: `{observed}`\n'
        f'- new: `{new}`\n'
        f'- candidates after local filters: `{candidates}`\n'
        f'- alerts: `{alerts}`\n\n'
        '**Top reject reasons:**\n'
        f'{top_rej}\n\n'
        f'**Auto-analysis:**\n{rec}\n\n'
        'Если много `empty pages` — проблема источника Steam/лимитов. Если много `weak float score` — фильтр float слишком строгий. Если много `Steam overpay` — лоты уже дороже обычной Steam цены.'
    )


def status_text(cfg: dict[str, Any], scanner: Scanner, chats: int) -> str:
    last = scanner.last_scan_summary or 'ещё не было скана после запуска'
    return (
        '**Статус мониторинга**\n'
        f'- enabled: `{cfg.get("enabled")}`\n'
        f'- interval: `{cfg.get("interval_seconds")}s`\n'
        f'- min price: `${cfg.get("min_price_usd", 0)}`\n'
        f'- max price: `${cfg.get("max_price_usd")}`\n'
        f'- min beautiful score: `{cfg.get("min_beautiful_score")}`\n'
        f'- allowed tiers: `{",".join(cfg.get("allowed_tiers", []))}`\n'
        f'- min resale ROI: `{cfg.get("min_resale_roi_pct", 10)}%`\n'
        f'- min float premium: `{cfg.get("min_float_premium_pct", 10)}%`\n'
        f'- max Steam overpay: `{cfg.get("max_steam_overpay_pct", 10)}%`\n'
        f'- low-float threshold: `{cfg.get("low_float_threshold")}`\n'
        f'- alert send delay: `{cfg.get("alert_send_delay_seconds", 2)}s`\n'
        f'- auto-safe: `{cfg.get("auto_safe_mode_enabled", True)}`; status: `{scanner.safe_mode_status()}`\n'
        f'- watchlist: `{len(sorted_watchlist(cfg))}`\n'
        f'- active chats: `{chats}`\n'
        f'- last scan: {last}\n\n'
        f'{health_summary(scanner)}'
    )


def settings_text(cfg: dict[str, Any], section: str = 'main') -> str:
    if section == 'price':
        return (
            '**Настройки → Цена Steam**\n'
            'Что меняем: минимальную и максимальную цену лота в Steam.\n'
            'На что влияет: бот не будет тратить время и слать алерты по скинам вне диапазона.\n\n'
            f'Сейчас: от `${cfg.get("min_price_usd", 0)}` до `${cfg.get("max_price_usd")}`.\n\n'
            'Пример: min $1 убирает копеечные $0.03 сигналы; max $100 защищает от слишком дорогих лотов.'
        )
    if section == 'tier':
        return (
            '**Настройки → Тиры float**\n'
            'Что меняем: какие уровни красоты float пропускать в алерты.\n'
            'На что влияет: чем меньше тиров включено, тем меньше шума и меньше CSFloat-запросов.\n\n'
            '💎 S — очень редкий/сильный паттерн\n'
            '🔥 A — сильный\n'
            '✨ B — средний, иногда интересен\n'
            '👀 C — слабый, чаще шум\n\n'
            f'Сейчас включены: `{",".join(cfg.get("allowed_tiers", []))}`.\n'
            'Практичный режим: S/A/B. Если сигналов слишком много — S/A.'
        )
    if section == 'edge':
        return (
            '**Настройки → ROI/премия**\n'
            'Что меняем: финансовые фильтры стратегии Steam → CSFloat.\n'
            'На что влияет: бот будет слать только те лоты, где есть шанс перепродажи, а не просто красивый float.\n\n'
            f'1) Min ROI: `{cfg.get("min_resale_roi_pct", 10)}%`\n'
            'Минимальная ожидаемая прибыль после 2% комиссии CSFloat.\n'
            'Пример: купили за $5, target sell $5.61 ≈ +10% ROI.\n\n'
            f'2) Min premium: `{cfg.get("min_float_premium_pct", 10)}%`\n'
            'Насколько похожие float должны быть дороже обычного CSFloat рынка.\n'
            'Пример: обычный CSFloat median $4.50, похожие float $5.40 → premium +20%.\n\n'
            f'3) Steam overpay: `≤ {cfg.get("max_steam_overpay_pct", 10)}%`\n'
            'Это максимальная переплата кандидата к обычной видимой Steam-цене.\n'
            'Пример: обычная Steam цена $5, лот $5.25 → overpay +5%; лот $7 → +40%, скорее продавец уже заложил premium.'
        )
    if section == 'float':
        return (
            '**Настройки → Красивый float**\n'
            'Что меняем: насколько красивым должен быть float, чтобы бот пошёл в CSFloat-аналитику.\n'
            'На что влияет: выше score = меньше сигналов, быстрее работа, чище качество.\n\n'
            'Score учитывает повторы, мемные числа, палиндромы и позицию паттерна.\n'
            f'Сейчас score ≥ `{cfg.get("min_beautiful_score")}`, low-float ≤ `{cfg.get("low_float_threshold")}`.\n\n'
            'Пример: score ≥55 — строгий режим; score ≥35 — нормальный тестовый режим.'
        )
    if section == 'time':
        return (
            '**Настройки → Время и скорость**\n'
            'Что меняем: частоту сканов, параллельность Steam-запросов, таймауты и паузу между Telegram-алертами.\n'
            'На что влияет: больше workers = быстрее обход большого watchlist, но выше риск таймаутов/лимитов Steam.\n\n'
            f'Скан каждые `{cfg.get("interval_seconds")}s`.\n'
            f'Steam workers `{cfg.get("steam_workers", 4)}` — сколько Steam-страниц проверять параллельно.\n'
            f'Steam timeout `{cfg.get("steam_timeout_seconds", 12)}s` — сколько ждать медленную страницу.\n'
            f'Задержка между алертами `{cfg.get("alert_send_delay_seconds", 2)}s`.\n'
            f'CSFloat cache `{cfg.get("csfloat_cache_ttl_seconds", 600)}s`.\n'
            f'Auto-safe: `{cfg.get("auto_safe_mode_enabled", True)}`; при деградации workers → `{cfg.get("safe_mode_workers", 3)}`, interval → `{cfg.get("safe_mode_interval_seconds", 120)}s`, cooldown `{cfg.get("safe_mode_duration_seconds", 900)}s`.\n'
            f'Rate-limit controller: `{cfg.get("rate_limit_controller_enabled", True)}`; group cooldown `{cfg.get("rate_limit_cooldown_seconds", 300)}–{cfg.get("rate_limit_cooldown_max_seconds", 1800)}s`, threshold `{cfg.get("rate_limit_empty_rate_pct", 50)}%`.\n\n'
            'Практично: workers 6 — нормальный режим; 10 — агрессивно; 3 — осторожно. Auto-safe и rate-limit controller сами снижают нагрузку, если Steam режет выдачу.'
        )
    return (
        '**Настройки**\n'
        'Выбери раздел ниже. Так меньше риска нажать не туда.\n\n'
        f'💰 Цена: `${cfg.get("min_price_usd", 0)}`–`${cfg.get("max_price_usd")}`\n'
        f'🏷 Тиры: `{",".join(cfg.get("allowed_tiers", []))}`\n'
        f'📈 ROI: `≥ {cfg.get("min_resale_roi_pct", 10)}%`; premium `≥ {cfg.get("min_float_premium_pct", 10)}%`; overpay `≤ {cfg.get("max_steam_overpay_pct", 10)}%`\n'
        f'🔢 Score: `≥ {cfg.get("min_beautiful_score")}`\n'
        f'⏱ Скан: `{cfg.get("interval_seconds")}s`, workers `{cfg.get("steam_workers", 4)}`, timeout `{cfg.get("steam_timeout_seconds", 12)}s`, cache `{cfg.get("csfloat_cache_ttl_seconds", 600)}s`, auto-safe `{cfg.get("auto_safe_mode_enabled", True)}`'
    )


def guide_text() -> str:
    return (
        '**Гайд по стратегии**\n'
        'Идея: продавец на Steam часто выставляет красивый float по обычной цене, потому что не смотрит float. Мы покупаем только если CSFloat показывает шанс продать дороже за счёт premium.\n\n'
        '**Строки алерта:**\n'
        'Price — цена покупки лота в Steam.\n'
        'Float — число конкретного лота; красивый float только повод проверить.\n'
        'Steam median — обычная видимая цена таких лотов на Steam. Хорошо, если кандидат около неё.\n'
        'Tier/score — сила float: 💎S > 🔥A > ✨B > 👀C.\n'
        'CSFloat normal — обычный рынок без premium за наш float.\n'
        'Похожие float / продажи — сколько стоят/продавались близкие float. Главное: n и median.\n'
        'Premium — насколько похожие float дороже обычного CSFloat рынка.\n'
        'Target sell — примерная цена, по которой можно пробовать продавать на CSFloat.\n'
        'ROI — прибыль после 2% комиссии CSFloat относительно покупки в Steam.\n\n'
        '**Быстрое решение:**\n'
        'SKIP: ROI < 10% или premium слабая.\n'
        'WATCH: идея есть, но мало продаж/компов.\n'
        'GOOD: ROI 25%+, есть компы и продажи.\n\n'
        'Формула: обычная Steam цена + красивый float + CSFloat premium + ROI после комиссии.'
    )


def last_alerts_text(con: sqlite3.Connection, limit: int = 10) -> str:
    rows = con.execute('''SELECT ts, listingid, market_hash_name, price_usd, float_value, beautiful_tier, beautiful_score, beautiful_reasons, url
                          FROM alerts ORDER BY id DESC LIMIT ?''', (limit,)).fetchall()
    if not rows:
        return 'Пока нет сохранённых сигналов.'
    chunks = ['Последние сигналы:']
    for i, (ts, listingid, name, price, fl, tier, score, reasons, url) in enumerate(rows, 1):
        chunks.append(f"\n{i}) {name}\nPrice: ${price}\nFloat: {fl}\nTier/score: {tier_badge(tier, score)}/{score}\nПричина: {translate_reasons(reasons)}\nListing ID: {listingid}\nURL: {url}\nTime: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts))}")
    return '\n'.join(chunks)


def help_text() -> str:
    return (
        "**CS2 Steam Float Watcher**\n"
        "Покупок не делает. Только мониторит Steam TP и шлёт алерты.\n\n"
        "Управление:\n"
        "- `/status` — состояние\n"
        "- `/settings` — настройки\n"
        "- `/watchlist` — получить сортированный CSV watchlist\n"
        "- отправь отредактированный CSV/TXT обратно — бот импортирует, новые предметы сначала проверит\n"
        "- `/add <market_hash_name>` — добавить один предмет с проверкой\n"
        "- `/last` — последние конкретные сигналы\n"
        "- `/scan` — ручной скан сейчас\n"
        "- `/pause` / `/resume` — пауза/старт\n\n"
        "Админ:\n"
        "- `/allow <user_id>` — добавить пользователя в whitelist\n"
        "- `/revoke <user_id>` — удалить\n"
        "- `/whitelist` — список пользователей\n"
    )


def main() -> None:
    env = load_env()
    token = env.get('STEAM_ALERT_BOT_TOKEN') or env.get('CS2_STEAM_ALERT_BOT_TOKEN')
    if not token:
        raise SystemExit(f'Missing STEAM_ALERT_BOT_TOKEN in {ENV_PATH}')
    con = init_db()
    bot = TelegramBot(token, con)
    me = bot.api('getMe')
    bot.setup_commands()
    log(f"Bot connected: @{me.get('result', {}).get('username')}")
    scanner = Scanner(con, bot)
    export_watchlist_files(load_config())
    threading.Thread(target=scanner.loop, daemon=True).start()
    bot.poll_loop(scanner)


if __name__ == '__main__':
    main()