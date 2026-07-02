#!/usr/bin/env python3
"""Steam Community Market watcher, read-only.

Monitors visible listings from Steam's redesigned market listing page for watchlisted CS2 items.
No buying, no login, no cookies.

Important limitation: the public listing page currently returns the visible/sorted page, not a guaranteed global
"newest listings" feed. This watcher detects listing IDs that are new to *our observed page*.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable

# local scorer
sys.path.insert(0, str(Path(__file__).resolve().parent))
from beautiful_float import score_float  # type: ignore

BASE = Path('/home/hermes/cs2-float-sniper')
DEFAULT_WATCHLIST = BASE / 'steam_watchlist.txt'
STATE_DB = BASE / 'steam_seen.sqlite'
OUT_DIR = BASE / 'output'

LISTING_SPLIT = r'\\\\\\"listingid\\\\\\":\\\\\\"'

@dataclass
class SteamListing:
    source_query: str
    listingid: str
    market_hash_name: str | None
    assetid: str | None
    price_usd: float | None
    fee_usd: float | None
    subtotal_text: str | None
    float_value: float | None
    beautiful_tier: str | None
    beautiful_score: int | None
    beautiful_reasons: str | None
    url: str


def steam_url(market_hash_name: str) -> str:
    return 'https://steamcommunity.com/market/listings/730/' + urllib.parse.quote(market_hash_name)


def fetch_html(market_hash_name: str, timeout: int = 25) -> str:
    req = urllib.request.Request(
        steam_url(market_hash_name),
        headers={
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/125 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9',
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode('utf-8', 'replace')


def unesc(s: str | None) -> str | None:
    if s is None:
        return None
    return s.replace('\\\\/', '/').replace('\\\\u0026', '&').replace('\\\\\"', '"')


def cents(v: str | None) -> float | None:
    if v is None:
        return None
    return round(int(v) / 100.0, 2)


def parse_listings(html: str, source_query: str) -> list[SteamListing]:
    parts = re.split(LISTING_SPLIT, html)
    rows: list[SteamListing] = []
    seen = set()
    for p in parts[1:]:
        listingid = p.split('\\\\\\"', 1)[0]
        if not listingid or listingid in seen:
            continue
        seen.add(listingid)
        unp = re.search(r'\\\\\\"unPrice\\\\\\":(\d+)', p)
        fee = re.search(r'\\\\\\"unFee\\\\\\":(\d+)', p)
        subtotal = re.search(r'\\\\\\"strSubtotal\\\\\\":\\\\\\"(.*?)\\\\\\"', p)
        mh = re.search(r'\\\\\\"market_hash_name\\\\\\":\\\\\\"(.*?)\\\\\\"', p)
        fv = re.search(r'\\\\\\"float_value\\\\\\":([0-9.]+)', p)
        asset = re.search(r'\\\\\\"assetid\\\\\\":\\\\\\"(\d+)\\\\\\"', p)
        float_val = float(fv.group(1)) if fv else None
        tier = score = reasons = None
        if float_val is not None:
            fs = score_float(str(float_val))
            tier, score, reasons = fs.tier, fs.score, '; '.join(fs.reasons)
        market_hash_name = unesc(mh.group(1)) if mh else None
        detail_url = None
        # Steam sometimes exposes a redesigned exact-lot URL like:
        # /market/listings/730/G...?...&assetproperty=...&detail=<listingid>
        # If that URL is present in the listing payload, prefer it over the generic item page.
        dm = re.search(r'(https?:\\\\/\\\\/steamcommunity\\\\.com\\\\/market\\\\/listings\\\\/730\\\\/G[^\\\\\"\s]+detail=' + re.escape(listingid) + r'[^\\\\\"\s]*)', p)
        if dm:
            detail_url = unesc(dm.group(1))
        if not detail_url:
            dm = re.search(r'(\\\\/market\\\\/listings\\\\/730\\\\/G[^\\\\\"\s]+detail=' + re.escape(listingid) + r'[^\\\\\"\s]*)', p)
            if dm:
                detail_url = 'https://steamcommunity.com' + (unesc(dm.group(1)) or '')
        # Fallback: item page + listing anchor. New Steam UI may ignore the anchor, but Listing ID remains in alert.
        url = detail_url or (steam_url(market_hash_name or source_query) + f'#listing_{listingid}')
        rows.append(SteamListing(
            source_query=source_query,
            listingid=listingid,
            market_hash_name=market_hash_name,
            assetid=asset.group(1) if asset else None,
            price_usd=cents(unp.group(1) if unp else None),
            fee_usd=cents(fee.group(1) if fee else None),
            subtotal_text=unesc(subtotal.group(1)) if subtotal else None,
            float_value=float_val,
            beautiful_tier=tier,
            beautiful_score=score,
            beautiful_reasons=reasons,
            url=url,
        ))
    return rows


def init_db(path: Path = STATE_DB) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
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
    return con


def mark_new(con: sqlite3.Connection, rows: Iterable[SteamListing]) -> list[SteamListing]:
    new=[]
    now=int(time.time())
    for r in rows:
        exists = con.execute('SELECT 1 FROM seen WHERE listingid=?', (r.listingid,)).fetchone()
        if not exists:
            new.append(r)
            con.execute('INSERT OR IGNORE INTO seen VALUES (?,?,?,?,?,?,?,?)', (
                r.listingid, now, r.source_query, r.market_hash_name, r.price_usd, r.float_value, r.beautiful_tier, r.beautiful_score
            ))
    con.commit()
    return new


def load_watchlist(path: Path) -> list[str]:
    return [x.strip() for x in path.read_text(encoding='utf-8').splitlines() if x.strip() and not x.strip().startswith('#')]


def interesting(r: SteamListing, min_score: int, max_price: float | None, low_float: float | None, min_price: float | None = None) -> bool:
    if min_price is not None and r.price_usd is not None and r.price_usd < min_price:
        return False
    if max_price is not None and r.price_usd is not None and r.price_usd > max_price:
        return False
    if r.beautiful_score is not None and r.beautiful_score >= min_score:
        return True
    if low_float is not None and r.float_value is not None and r.float_value <= low_float:
        return True
    return False


def write_report(all_rows: list[SteamListing], new_rows: list[SteamListing], interesting_rows: list[SteamListing]) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tag=time.strftime('%Y%m%d_%H%M%S')
    path=OUT_DIR / f'steam_watch_{tag}.md'
    lines=['# Steam watcher report', '', f'- observed listings: {len(all_rows)}', f'- new-to-db listings: {len(new_rows)}', f'- interesting listings: {len(interesting_rows)}', '', '## Interesting', '', '| Item | Price | Float | Tier | Score | Listing | Reasons |', '|---|---:|---:|---|---:|---|---|']
    for r in interesting_rows[:100]:
        lines.append(f"| {r.market_hash_name or ''} | ${r.price_usd if r.price_usd is not None else '—'} | {r.float_value if r.float_value is not None else '—'} | {r.beautiful_tier or ''} | {r.beautiful_score if r.beautiful_score is not None else ''} | `{r.listingid}` | {r.beautiful_reasons or ''} |")
    lines += ['', '## New sample', '', '| Item | Price | Float | Tier | Score | Listing |', '|---|---:|---:|---|---:|---|']
    for r in new_rows[:50]:
        lines.append(f"| {r.market_hash_name or ''} | ${r.price_usd if r.price_usd is not None else '—'} | {r.float_value if r.float_value is not None else '—'} | {r.beautiful_tier or ''} | {r.beautiful_score if r.beautiful_score is not None else ''} | `{r.listingid}` |")
    path.write_text('\n'.join(lines)+'\n', encoding='utf-8')
    csv_path=path.with_suffix('.csv')
    with csv_path.open('w', newline='', encoding='utf-8') as f:
        w=csv.DictWriter(f, fieldnames=list(asdict(all_rows[0]).keys()) if all_rows else ['listingid'])
        w.writeheader()
        for r in all_rows:
            w.writerow(asdict(r))
    return path


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--watchlist', default=str(DEFAULT_WATCHLIST))
    ap.add_argument('--min-score', type=int, default=35, help='Beautiful float score threshold')
    ap.add_argument('--low-float', type=float, default=0.01, help='Also flag floats <= threshold')
    ap.add_argument('--min-price', type=float, default=0.0)
    ap.add_argument('--max-price', type=float, default=100.0)
    ap.add_argument('--delay', type=float, default=3.0)
    ap.add_argument('--reset-seen', action='store_true')
    args=ap.parse_args()

    if args.reset_seen and STATE_DB.exists():
        STATE_DB.unlink()
    con=init_db()
    watch=load_watchlist(Path(args.watchlist))
    all_rows=[]
    for i,name in enumerate(watch,1):
        print(f'[{i}/{len(watch)}] fetch {name}')
        try:
            html=fetch_html(name)
            rows=parse_listings(html, name)
            print(f'  parsed {len(rows)} visible listings')
            all_rows.extend(rows)
        except Exception as e:
            print(f'  ERROR {type(e).__name__}: {e}')
        time.sleep(args.delay)
    new_rows=mark_new(con, all_rows)
    interesting_rows=[r for r in new_rows if interesting(r, args.min_score, args.max_price, args.low_float, args.min_price)]
    report=write_report(all_rows, new_rows, interesting_rows) if all_rows else None
    print('observed', len(all_rows), 'new', len(new_rows), 'interesting', len(interesting_rows))
    if report:
        print('report', report)
        print('csv', report.with_suffix('.csv'))

if __name__ == '__main__':
    main()
