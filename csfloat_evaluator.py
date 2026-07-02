#!/usr/bin/env python3
"""CSFloat evaluator: compare normal floor vs low-float floors.

Reads API key from /home/hermes/.config/cs2-float-sniper/.env
Does NOT print the key.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from beautiful_float import score_float  # type: ignore
except Exception:  # keep evaluator usable standalone without local scorer
    score_float = None  # type: ignore

CONFIG = Path('/home/hermes/.config/cs2-float-sniper/.env')
OUT_DIR = Path('/home/hermes/cs2-float-sniper/output')
BASE_URL = 'https://csfloat.com/api/v1'

DEFAULT_ITEMS = [
    'Desert Eagle | Mecha Industries (Factory New)',
    'Desert Eagle | Conspiracy (Factory New)',
    'AK-47 | Slate (Factory New)',
    'AK-47 | Redline (Field-Tested)',
    'AWP | Atheris (Factory New)',
    'AWP | Neo-Noir (Factory New)',
    'M4A1-S | Decimator (Factory New)',
    'M4A4 | In Living Color (Factory New)',
    'USP-S | Cortex (Factory New)',
    'Glock-18 | Vogue (Factory New)',
    'Five-SeveN | Angry Mob (Factory New)',
    'P250 | Asiimov (Field-Tested)',
    'MP9 | Food Chain (Factory New)',
    'MAC-10 | Disco Tech (Factory New)',
    'Galil AR | Chromatic Aberration (Factory New)',
    'FAMAS | ZX Spectron (Factory New)',
]

THRESHOLDS = [0.01, 0.005, 0.001]
CSFLOAT_SELL_FEE_MULTIPLIER = 0.98
DEFAULT_STEAM_MARKUP_MIN = 1.10
DEFAULT_STEAM_MARKUP_MAX = 1.15


def beauty_multiplier_from_score(score: int | None, reasons: str | list[str] | None = None) -> float:
    """Conservative expected CSFloat premium for collector-friendly float digits.

    This is not a guaranteed price. It prevents the scanner from rejecting Steam lots
    that are base-priced versus nearby CSFloat floats, but have a clearly better
    collector number such as 0.666666, 0.0007777, 0.00420420, etc.
    """
    if score is None:
        return 1.00

    # Base multiplier by score tier. Keep this conservative: alerts are for manual review.
    if score >= 95:
        mult = 1.60
    elif score >= 85:
        mult = 1.45
    elif score >= 75:
        mult = 1.35
    elif score >= 65:
        mult = 1.25
    elif score >= 55:
        mult = 1.18
    elif score >= 35:
        mult = 1.10
    elif score >= 18:
        mult = 1.04
    else:
        mult = 1.00

    # Small extra bump for the patterns that are easiest to resell to collectors.
    joined = '; '.join(reasons) if isinstance(reasons, list) else (reasons or '')
    low = joined.lower()
    if score >= 55 and ('6x repeated' in low or '7x repeated' in low or '8x repeated' in low):
        mult += 0.07
    if score >= 55 and ('meme pattern 1337' in low or 'meme pattern 0420' in low or 'meme pattern 0069' in low):
        mult += 0.05
    if score >= 55 and ('ultra low' in low and 'repeated digit' in low):
        mult += 0.05

    return round(min(mult, 2.00), 3)


def infer_float_beauty(target_float: float | None) -> dict[str, Any]:
    if target_float is None or score_float is None:
        return {'beautiful_score': None, 'beautiful_tier': None, 'beautiful_reasons': None, 'beauty_multiplier': 1.0}
    try:
        fs = score_float(str(target_float))
        reasons = '; '.join(fs.reasons)
        return {
            'beautiful_score': fs.score,
            'beautiful_tier': fs.tier,
            'beautiful_reasons': reasons,
            'beauty_multiplier': beauty_multiplier_from_score(fs.score, fs.reasons),
        }
    except Exception:
        return {'beautiful_score': None, 'beautiful_tier': None, 'beautiful_reasons': None, 'beauty_multiplier': 1.0}


def load_api_key() -> str:
    if not CONFIG.exists():
        raise SystemExit(f'Config not found: {CONFIG}')
    for line in CONFIG.read_text(encoding='utf-8').splitlines():
        if line.strip().startswith('CSFLOAT_API_KEY='):
            return line.split('=', 1)[1].strip().strip('"').strip("'")
    raise SystemExit('CSFLOAT_API_KEY not found in config')


class CSFloat:
    def __init__(self, api_key: str, delay: float = 0.25):
        self.api_key = api_key
        self.delay = delay

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any] | list[Any]:
        if params:
            qs = urllib.parse.urlencode(params, doseq=True)
            url = f'{BASE_URL}{path}?{qs}'
        else:
            url = f'{BASE_URL}{path}'
        req = urllib.request.Request(url)
        req.add_header('Authorization', self.api_key)
        req.add_header('User-Agent', 'cs2-float-sniper-research/0.1')
        time.sleep(self.delay)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', 'replace')[:300]
            return {'error': f'HTTP {e.code}: {body}'}
        except Exception as e:
            return {'error': f'{type(e).__name__}: {e}'}

    def listings(self, market_hash_name: str, *, min_float: float | None = None, max_float: float | None = None, sort_by: str = 'lowest_price', limit: int = 20) -> dict[str, Any]:
        params: dict[str, Any] = {
            'market_hash_name': market_hash_name,
            'sort_by': sort_by,
            'limit': min(limit, 50),
            'category': 0,
            'type': 'buy_now',
        }
        if min_float is not None:
            params['min_float'] = min_float
        if max_float is not None:
            params['max_float'] = max_float
        data = self.get('/listings', params)
        if isinstance(data, dict):
            return data
        return {'error': 'unexpected non-dict response'}

    def sales(self, market_hash_name: str) -> Any:
        # API expects path segment; quote slashes/pipes/spaces safely.
        name = urllib.parse.quote(market_hash_name, safe='')
        return self.get(f'/history/{name}/sales')


def cents_to_usd(v: int | float | None) -> float | None:
    if v is None:
        return None
    return round(float(v) / 100.0, 2)


def extract_prices(listings: list[dict[str, Any]]) -> list[int]:
    prices = []
    for row in listings:
        p = row.get('price')
        if isinstance(p, (int, float)):
            prices.append(int(p))
    return prices


def extract_float(row: dict[str, Any]) -> float | None:
    item = row.get('item') or {}
    fv = item.get('float_value')
    return float(fv) if isinstance(fv, (int, float)) else None




def price_stats_cents(prices: list[int]) -> dict[str, Any]:
    if not prices:
        return {'count': 0}
    vals = sorted(prices)
    def pct(q: float) -> int:
        if len(vals) == 1:
            return vals[0]
        pos = (len(vals) - 1) * q
        lo = int(pos)
        hi = min(lo + 1, len(vals) - 1)
        w = pos - lo
        return round(vals[lo] * (1 - w) + vals[hi] * w)
    return {
        'count': len(vals),
        'min_usd': cents_to_usd(vals[0]),
        'p25_usd': cents_to_usd(pct(0.25)),
        'median_usd': cents_to_usd(round(statistics.median(vals))),
        'avg_usd': cents_to_usd(round(statistics.mean(vals))),
        'p75_usd': cents_to_usd(pct(0.75)),
        'max_usd': cents_to_usd(vals[-1]),
    }


def extract_listing_comp(row: dict[str, Any]) -> dict[str, Any]:
    item = row.get('item') or {}
    ref = row.get('reference') or {}
    return {
        'id': row.get('id'),
        'price_usd': cents_to_usd(row.get('price')),
        'float_value': extract_float(row),
        'url': f"https://csfloat.com/item/{row.get('id')}" if row.get('id') else None,
        'reference_base_usd': cents_to_usd(ref.get('base_price')),
        'reference_predicted_usd': cents_to_usd(ref.get('predicted_price')),
        'reference_quantity': ref.get('quantity'),
    }


def evaluate_nearby_float(
    client: CSFloat,
    name: str,
    target_float: float | None,
    buy_price_usd: float | None = None,
    *,
    min_comps: int = 5,
    limit: int = 20,
    beautiful_score: int | None = None,
    beautiful_reasons: str | list[str] | None = None,
    beautiful_tier: str | None = None,
    steam_markup_min: float = DEFAULT_STEAM_MARKUP_MIN,
    steam_markup_max: float = DEFAULT_STEAM_MARKUP_MAX,
) -> dict[str, Any]:
    """Evaluate a Steam candidate against CSFloat comps near its actual float.

    This intentionally avoids single low-float floors. It expands symmetric float windows
    around the target until enough comparable active listings are found, then reports
    median/average/range and recent sold comps from CSFloat history.
    """
    inferred_beauty = infer_float_beauty(target_float)
    if beautiful_score is None:
        beautiful_score = inferred_beauty.get('beautiful_score')
    if beautiful_tier is None:
        beautiful_tier = inferred_beauty.get('beautiful_tier')
    if beautiful_reasons is None:
        beautiful_reasons = inferred_beauty.get('beautiful_reasons')
    beauty_multiplier = beauty_multiplier_from_score(beautiful_score, beautiful_reasons)

    result: dict[str, Any] = {
        'market_hash_name': name,
        'target_float': target_float,
        'buy_price_usd': buy_price_usd,
        'mode': 'nearby_float_comps_with_beauty_premium',
        'beautiful_score': beautiful_score,
        'beautiful_tier': beautiful_tier,
        'beautiful_reasons': beautiful_reasons,
        'beauty_multiplier': beauty_multiplier,
        'steam_markup_min': steam_markup_min,
        'steam_markup_max': steam_markup_max,
    }
    normal = client.listings(name, sort_by='lowest_price', limit=limit)
    normal_rows = normal.get('data') or [] if isinstance(normal, dict) else []
    normal_prices = extract_prices(normal_rows)
    result['active_listings_sample'] = len(normal_rows)
    result['active_floor_usd'] = cents_to_usd(min(normal_prices) if normal_prices else None)
    result['active_median_10_usd'] = cents_to_usd(round(statistics.median(normal_prices[:10]))) if normal_prices else None
    result['reference_quantity'] = (normal_rows[0].get('reference') or {}).get('quantity') if normal_rows else None
    if target_float is None:
        result['risk_flags'] = 'NO_TARGET_FLOAT'
        return result
    windows = [0.0025, 0.005, 0.01, 0.025, 0.05, 0.10]
    selected_rows: list[dict[str, Any]] = []
    selected_window = None
    for w in windows:
        lo = max(0.0, target_float - w)
        hi = min(1.0, target_float + w)
        data = client.listings(name, min_float=lo, max_float=hi, sort_by='lowest_price', limit=limit)
        rows = data.get('data') or [] if isinstance(data, dict) else []
        if rows and (len(rows) >= min_comps or w == windows[-1]):
            selected_rows = rows
            selected_window = w
            break
    comps = [extract_listing_comp(r) for r in selected_rows]
    prices = [int(r.get('price')) for r in selected_rows if isinstance(r.get('price'), (int, float))]
    floats = [extract_float(r) for r in selected_rows if extract_float(r) is not None]
    result['active_comp_window'] = selected_window
    result['active_comp_float_min'] = min(floats) if floats else None
    result['active_comp_float_max'] = max(floats) if floats else None
    result['active_comp_stats'] = price_stats_cents(prices)
    result['active_comp_examples'] = comps[:5]
    sales_raw = client.sales(name)
    sales_rows = sales_raw if isinstance(sales_raw, list) else []
    sale_comps = []
    sale_window = None
    for w in windows:
        lo = max(0.0, target_float - w)
        hi = min(1.0, target_float + w)
        rows = [r for r in sales_rows if (fv := extract_float(r)) is not None and lo <= fv <= hi]
        if rows and (len(rows) >= min_comps or w == windows[-1]):
            sale_comps = rows[:limit]
            sale_window = w
            break
    sale_prices = [int(r.get('price')) for r in sale_comps if isinstance(r.get('price'), (int, float))]
    sale_floats = [extract_float(r) for r in sale_comps if extract_float(r) is not None]
    result['recent_sales_sample_total'] = len(sales_rows)
    result['recent_sale_window'] = sale_window
    result['recent_sale_float_min'] = min(sale_floats) if sale_floats else None
    result['recent_sale_float_max'] = max(sale_floats) if sale_floats else None
    result['recent_sale_stats'] = price_stats_cents(sale_prices)
    result['recent_sale_examples'] = [extract_listing_comp(r) for r in sale_comps[:5]]
    target_sell = None
    source = None
    if result['recent_sale_stats'].get('count', 0) >= 3:
        target_sell = result['recent_sale_stats'].get('median_usd')
        source = 'recent_sales_near_float_median'
    elif result['active_comp_stats'].get('count', 0) >= 3:
        target_sell = result['active_comp_stats'].get('median_usd')
        source = 'active_near_float_median'
    elif result['active_median_10_usd'] is not None:
        target_sell = result['active_median_10_usd']
        source = 'active_general_median_10_fallback'
    normal_cs = result.get('active_median_10_usd') or result.get('active_floor_usd')

    # Base comparable value: what a normal nearby-float item is worth on CSFloat.
    base_target_sell = target_sell

    # Expected collector resale value: nearby-float base * beauty multiplier.
    # Backward-compatible fields intentionally use the beauty-adjusted target, because
    # steam_alert_bot reads target_sell_price_usd and recalculates resale_roi_pct.
    expected_target_sell = round(float(base_target_sell) * beauty_multiplier, 2) if base_target_sell is not None else None

    result['base_target_sell_price_usd'] = base_target_sell
    result['target_sell_price_usd'] = expected_target_sell
    result['estimated_fair_usd'] = expected_target_sell  # backward-compatible alias
    result['valuation_source'] = source
    result['valuation_note'] = 'target_sell_price_usd includes beauty_multiplier; base_target_sell_price_usd is raw nearby-float comp median'

    if base_target_sell is not None:
        result['steam_fair_min_usd'] = round(float(base_target_sell) * float(steam_markup_min), 2)
        result['steam_fair_max_usd'] = round(float(base_target_sell) * float(steam_markup_max), 2)
    else:
        result['steam_fair_min_usd'] = None
        result['steam_fair_max_usd'] = None

    if base_target_sell is not None and buy_price_usd:
        result['steam_vs_csfloat_base_pct'] = round((float(buy_price_usd) / float(base_target_sell) - 1) * 100, 1)
        result['steam_base_price_ok'] = float(buy_price_usd) <= float(base_target_sell) * float(steam_markup_max)
    else:
        result['steam_vs_csfloat_base_pct'] = None
        result['steam_base_price_ok'] = None

    if base_target_sell is not None and buy_price_usd:
        result['base_resale_roi_pct'] = round((float(base_target_sell) * CSFLOAT_SELL_FEE_MULTIPLIER / float(buy_price_usd) - 1) * 100, 1)
        result['base_gross_resale_pct'] = round((float(base_target_sell) / float(buy_price_usd) - 1) * 100, 1)
    else:
        result['base_resale_roi_pct'] = None
        result['base_gross_resale_pct'] = None

    if expected_target_sell is not None and buy_price_usd:
        result['resale_roi_pct'] = round((float(expected_target_sell) * CSFLOAT_SELL_FEE_MULTIPLIER / float(buy_price_usd) - 1) * 100, 1)
        result['gross_resale_pct'] = round((float(expected_target_sell) / float(buy_price_usd) - 1) * 100, 1)
        result['net_edge_after_2pct_fee_pct'] = result['resale_roi_pct']  # backward-compatible alias
    else:
        result['resale_roi_pct'] = None
        result['gross_resale_pct'] = None
        result['net_edge_after_2pct_fee_pct'] = None

    if base_target_sell is not None and normal_cs:
        result['base_float_premium_pct'] = round((float(base_target_sell) / float(normal_cs) - 1) * 100, 1)
    else:
        result['base_float_premium_pct'] = None
    if expected_target_sell is not None and normal_cs:
        result['float_premium_pct'] = round((float(expected_target_sell) / float(normal_cs) - 1) * 100, 1)
    else:
        result['float_premium_pct'] = None

    if buy_price_usd:
        result['break_even_sell_price_usd'] = round(float(buy_price_usd) / CSFLOAT_SELL_FEE_MULTIPLIER, 2)
        result['sell_price_for_10pct_roi_usd'] = round(float(buy_price_usd) * 1.10 / CSFLOAT_SELL_FEE_MULTIPLIER, 2)
        result['sell_price_for_20pct_roi_usd'] = round(float(buy_price_usd) * 1.20 / CSFLOAT_SELL_FEE_MULTIPLIER, 2)
    else:
        result['break_even_sell_price_usd'] = None
        result['sell_price_for_10pct_roi_usd'] = None
        result['sell_price_for_20pct_roi_usd'] = None
    flags = []
    if result['active_comp_stats'].get('count', 0) < min_comps:
        flags.append('МАЛО АКТИВНЫХ КОМПОВ')
    if result['recent_sale_stats'].get('count', 0) < 3:
        flags.append('МАЛО ПРОДАЖ РЯДОМ С FLOAT')
    if result['reference_quantity'] is not None and result['reference_quantity'] < 20:
        flags.append('НИЗКАЯ ЛИКВИДНОСТЬ')
    if result.get('steam_base_price_ok') is False:
        flags.append('STEAM ДОРОЖЕ CSFLOAT-БАЗЫ С УЧЕТОМ 15%')
    if result['resale_roi_pct'] is not None and result['resale_roi_pct'] < 10:
        flags.append('BEAUTY-ADJUSTED ROI МЕНЬШЕ 10%')
    if result['float_premium_pct'] is not None and result['float_premium_pct'] < 10:
        flags.append('BEAUTY-ПРЕМИЯ К CSFLOAT РЫНКУ МЕНЬШЕ 10%')
    result['risk_flags'] = '; '.join(flags) if flags else '—'
    return result

def evaluate_item(client: CSFloat, name: str) -> dict[str, Any]:
    normal = client.listings(name, sort_by='lowest_price', limit=20)
    normal_rows = normal.get('data') or [] if isinstance(normal, dict) else []
    normal_prices = extract_prices(normal_rows)
    normal_floor = min(normal_prices) if normal_prices else None
    normal_second = sorted(normal_prices)[1] if len(normal_prices) > 1 else None
    normal_median = statistics.median(normal_prices[:10]) if normal_prices else None

    result: dict[str, Any] = {
        'market_hash_name': name,
        'normal_count_sample': len(normal_rows),
        'normal_floor_usd': cents_to_usd(normal_floor),
        'normal_second_floor_usd': cents_to_usd(normal_second),
        'normal_median_10_usd': cents_to_usd(normal_median),
    }

    for th in THRESHOLDS:
        lf = client.listings(name, max_float=th, sort_by='lowest_price', limit=20)
        rows = lf.get('data') or [] if isinstance(lf, dict) else []
        prices = extract_prices(rows)
        floor = min(prices) if prices else None
        result[f'lf_{th}_count_sample'] = len(rows)
        result[f'lf_{th}_floor_usd'] = cents_to_usd(floor)
        if floor and normal_floor:
            result[f'lf_{th}_premium_pct'] = round((floor / normal_floor - 1) * 100, 1)
        else:
            result[f'lf_{th}_premium_pct'] = None
        if rows:
            best = min(rows, key=lambda r: r.get('price', 10**18))
            result[f'lf_{th}_best_float'] = extract_float(best)
            result[f'lf_{th}_best_url'] = f"https://csfloat.com/item/{best.get('id')}"
        else:
            result[f'lf_{th}_best_float'] = None
            result[f'lf_{th}_best_url'] = None

    # Conservative flags
    flags = []
    if not normal_prices:
        flags.append('NO_NORMAL_LISTINGS')
    if normal_prices and len(normal_prices) < 5:
        flags.append('LOW_NORMAL_SAMPLE')
    if not result.get('lf_0.01_floor_usd'):
        flags.append('NO_LOW_FLOAT_0.01_LISTINGS')
    result['risk_flags'] = ','.join(flags)
    return result


def write_outputs(rows: list[dict[str, Any]], tag: str) -> tuple[Path, Path]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = OUT_DIR / f'csfloat_evaluation_{tag}.csv'
    md_path = OUT_DIR / f'csfloat_evaluation_{tag}.md'
    fieldnames = list(rows[0].keys()) if rows else []
    with csv_path.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    lines = ['# CSFloat evaluator report', '', '| Item | Floor | LF<0.01 floor | Premium | LF<0.005 floor | Premium | LF<0.001 floor | Premium | Flags |', '|---|---:|---:|---:|---:|---:|---:|---:|---|']
    for r in rows:
        lines.append(
            f"| {r['market_hash_name']} | ${r.get('normal_floor_usd') or '—'} | "
            f"${r.get('lf_0.01_floor_usd') or '—'} | {r.get('lf_0.01_premium_pct') if r.get('lf_0.01_premium_pct') is not None else '—'}% | "
            f"${r.get('lf_0.005_floor_usd') or '—'} | {r.get('lf_0.005_premium_pct') if r.get('lf_0.005_premium_pct') is not None else '—'}% | "
            f"${r.get('lf_0.001_floor_usd') or '—'} | {r.get('lf_0.001_premium_pct') if r.get('lf_0.001_premium_pct') is not None else '—'}% | "
            f"{r.get('risk_flags') or ''} |"
        )
    md_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return csv_path, md_path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--items-file', help='Text file with one market_hash_name per line')
    ap.add_argument('--limit', type=int, default=16)
    args = ap.parse_args()
    if args.items_file:
        items = [x.strip() for x in Path(args.items_file).read_text(encoding='utf-8').splitlines() if x.strip() and not x.strip().startswith('#')]
    else:
        items = DEFAULT_ITEMS
    items = items[:args.limit]
    client = CSFloat(load_api_key())
    rows = []
    for i, name in enumerate(items, 1):
        print(f'[{i}/{len(items)}] {name}')
        rows.append(evaluate_item(client, name))
    tag = time.strftime('%Y%m%d_%H%M%S')
    csv_path, md_path = write_outputs(rows, tag)
    print('csv', csv_path)
    print('md', md_path)


if __name__ == '__main__':
    main()