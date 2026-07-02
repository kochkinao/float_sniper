#!/usr/bin/env python3
"""Beautiful / rare CS2 float scorer.

The scorer is intentionally permissive: it is a signal generator for manual
review, not a price guarantee. Its main job is to avoid missing collector-style
float numbers that sellers often list for a normal Steam price.

Detects, among others:
- long repeated digits: 0.666666, 0.00666666, 0.0007777
- meme numbers: 1337, 420, 69, 666, 777, 555, 888
- periodic patterns: 0.69696969, 0.42042042, 0.12312312
- palindromes / mirrored chunks: 0.1234321, 0.1000001
- sequences: 0.012345, 0.987654
- ultra-low floats with many leading zeros
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Iterable

# Longer/more specific patterns first. The scorer still allows several patterns
# to stack, but caps repeated meme stacking so 666666 is not counted as infinite 666s.
MEME_PATTERNS: dict[str, int] = {
    '01337': 22,
    '001337': 26,
    '1337': 18,
    '0420': 14,
    '00420': 18,
    '000420': 22,
    '420': 10,
    '0069': 14,
    '00069': 18,
    '069': 10,
    '69': 5,
    '666': 14,
    '777': 14,
    '555': 11,
    '888': 11,
    '999': 9,
    '111': 8,
    '222': 8,
    '333': 8,
    '444': 8,
    '0001': 8,
    '1000': 6,
}

# Collector-friendly two/three digit units. These are weighted lower than long
# raw repeated runs, but they catch floats like 0.69696969 and 0.42042042.
GOOD_PERIOD_UNITS = {
    '69': 14,
    '42': 9,
    '420': 16,
    '1337': 18,
    '13': 6,
    '37': 5,
    '66': 8,
    '77': 8,
    '88': 6,
    '55': 6,
    '123': 11,
    '321': 10,
    '01': 5,
    '10': 5,
}

@dataclass
class FloatScore:
    float_value: str
    score: int
    tier: str
    reasons: list[str]
    decimal_digits: str


def normalize_float(value: str | float) -> tuple[str, str]:
    """Return display value and decimal digits without leading ``0.``.

    Decimal is used so values such as ``1e-06`` do not lose the exact decimal
    representation. Trailing zeros are removed because Steam/API floats usually
    arrive without meaningful trailing zero precision.
    """
    s = str(value).strip()
    try:
        d = Decimal(s)
    except InvalidOperation:
        raise ValueError(f'Invalid float: {value}')
    if d < 0 or d > 1:
        raise ValueError(f'Float must be between 0 and 1: {value}')
    fixed = format(d, 'f')
    if '.' not in fixed:
        fixed += '.0'
    int_part, dec = fixed.split('.', 1)
    dec = dec.rstrip('0') or '0'
    return f'{int_part}.{dec}', dec


def longest_run(s: str) -> tuple[str, int, int]:
    best_digit = ''
    best_len = 0
    best_pos = 0
    i = 0
    while i < len(s):
        j = i + 1
        while j < len(s) and s[j] == s[i]:
            j += 1
        run_len = j - i
        if run_len > best_len:
            best_digit = s[i]
            best_len = run_len
            best_pos = i
        i = j
    return best_digit, best_len, best_pos


def leading_zeros(dec: str) -> int:
    return len(dec) - len(dec.lstrip('0'))


def windows(s: str, min_len: int, max_len: int) -> Iterable[tuple[int, str]]:
    max_len = min(max_len, len(s))
    for n in range(max_len, min_len - 1, -1):
        for i in range(0, len(s) - n + 1):
            yield i, s[i:i+n]


def is_palindrome_chunk(dec: str, min_len: int = 5) -> str | None:
    # Prefer early / longer chunks, but allow any strong window.
    for _, chunk in windows(dec, min_len, 8):
        if chunk == chunk[::-1]:
            return chunk
    return None


def mirror_chunk(dec: str) -> str | None:
    # Catches ABCCBA / ABCDDCBA-like structure even inside a longer float.
    for _, chunk in windows(dec, 6, 8):
        if chunk == chunk[::-1] and len(set(chunk)) > 1:
            return chunk
    return None


def sequence_chunk(dec: str) -> str | None:
    sequences = ['0123456789', '123456789', '9876543210', '987654321']
    for seq in sequences:
        for n in range(min(len(seq), 8), 4, -1):
            for i in range(0, len(seq) - n + 1):
                chunk = seq[i:i+n]
                if chunk in dec:
                    return chunk
    return None


def repeated_unit_chunk(dec: str) -> tuple[str, int, int] | None:
    """Return (unit, repeats, pos) for 2-4 digit periodic patterns.

    Examples: 696969, 420420, 123123, 010101.
    """
    best: tuple[str, int, int] | None = None
    best_score = 0
    for unit_len in (2, 3, 4):
        for pos in range(0, max(0, len(dec) - unit_len * 2 + 1)):
            unit = dec[pos:pos+unit_len]
            if len(unit) < unit_len:
                continue
            repeats = 1
            k = pos + unit_len
            while dec[k:k+unit_len] == unit:
                repeats += 1
                k += unit_len
            if repeats < 2:
                continue
            covered = repeats * unit_len
            # 2 repeats of a 2-digit unit is too weak unless it is a known unit.
            if covered < 6 and unit not in GOOD_PERIOD_UNITS:
                continue
            score = covered + repeats * 2 + (4 if pos <= 2 else 0) + GOOD_PERIOD_UNITS.get(unit, 0)
            if score > best_score:
                best_score = score
                best = (unit, repeats, pos)
    return best


def repeated_prefix_suffix(dec: str) -> str | None:
    # Catches bookended / mirrored-looking floats such as 1000001, 42000420.
    for n in range(4, 1, -1):
        if len(dec) >= n * 2 + 1 and dec[:n] == dec[-n:]:
            return dec[:n]
    return None


def first_significant_block(dec: str) -> str:
    return dec.lstrip('0')


def score_float(value: str | float) -> FloatScore:
    display, dec = normalize_float(value)
    reasons: list[str] = []
    score = 0

    lz = leading_zeros(dec)
    if lz >= 5:
        add = 35 + (lz - 5) * 8
        score += add
        reasons.append(f'ultra low: {lz} leading zeros (+{add})')
    elif lz == 4:
        score += 25
        reasons.append('very low: 4 leading zeros (+25)')
    elif lz == 3:
        score += 15
        reasons.append('low: 3 leading zeros (+15)')
    elif lz == 2:
        score += 8
        reasons.append('2 leading zeros (+8)')

    digit, run_len, pos = longest_run(dec)
    if run_len >= 8:
        add = 55 + (run_len - 8) * 10
        score += add
        reasons.append(f'{run_len}x repeated digit {digit} (+{add})')
    elif run_len == 7:
        score += 45
        reasons.append(f'7x repeated digit {digit} (+45)')
    elif run_len == 6:
        score += 35
        reasons.append(f'6x repeated digit {digit} (+35)')
    elif run_len == 5:
        score += 25
        reasons.append(f'5x repeated digit {digit} (+25)')
    elif run_len == 4:
        score += 14
        reasons.append(f'4x repeated digit {digit} (+14)')
    elif run_len == 3:
        score += 6
        reasons.append(f'3x repeated digit {digit} (+6)')

    if run_len >= 4 and pos <= 3:
        score += 8
        reasons.append('main repeat appears early (+8)')

    # Meme patterns: allow one high-value meme and a couple of small supporting ones.
    meme_hits = 0
    high_meme_hit = False
    for pat, pts in MEME_PATTERNS.items():
        if pat in dec:
            if pts >= 14:
                if high_meme_hit:
                    continue
                high_meme_hit = True
            elif meme_hits >= 3:
                continue
            score += pts
            meme_hits += 1
            reasons.append(f'meme pattern {pat} (+{pts})')

    periodic = repeated_unit_chunk(dec)
    if periodic:
        unit, repeats, unit_pos = periodic
        covered = len(unit) * repeats
        add = 12 + max(0, covered - 6) * 2 + GOOD_PERIOD_UNITS.get(unit, 0)
        if unit_pos <= 2:
            add += 5
        add = min(add, 35)
        score += add
        reasons.append(f'periodic pattern {unit}x{repeats} (+{add})')

    pal = is_palindrome_chunk(dec)
    if pal:
        add = 14 if len(pal) >= 7 else 12
        score += add
        reasons.append(f'palindrome {pal} (+{add})')

    seq = sequence_chunk(dec)
    if seq:
        add = 18 if len(seq) >= 7 else 14
        score += add
        reasons.append(f'sequence {seq} (+{add})')

    bookend = repeated_prefix_suffix(dec)
    if bookend:
        add = 10 + len(bookend) * 2
        score += add
        reasons.append(f'bookend repeat {bookend}...{bookend} (+{add})')

    stripped = first_significant_block(dec)
    if len(stripped) >= 4 and len(set(stripped[:4])) == 1:
        score += 10
        reasons.append(f'first significant digits repeat {stripped[:4]} (+10)')

    if len(stripped) >= 6 and stripped[:3] == stripped[3:6]:
        score += 12
        reasons.append(f'first significant triplet repeats {stripped[:3]}{stripped[:3]} (+12)')

    # Strong combo bonus: collector numbers become much more interesting when the
    # same float has several independent hooks.
    strong_hooks = sum(
        1 for r in reasons
        if any(key in r for key in ('repeated digit', 'meme pattern', 'periodic pattern', 'palindrome', 'sequence', 'ultra low'))
    )
    if strong_hooks >= 3:
        combo = min(18, (strong_hooks - 2) * 6)
        score += combo
        reasons.append(f'collector combo bonus (+{combo})')

    if score >= 80:
        tier = 'S'
    elif score >= 55:
        tier = 'A'
    elif score >= 35:
        tier = 'B'
    elif score >= 18:
        tier = 'C'
    else:
        tier = 'normal'

    if not reasons:
        reasons.append('no collector-number pattern')
    return FloatScore(display, int(score), tier, reasons, dec)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('floats', nargs='*')
    args = ap.parse_args()
    vals = args.floats or [
        '0.666666', '0.00666666', '0.0007777', '0.001337',
        '0.000420', '0.000069', '0.012345', '0.054321',
        '0.00555555', '0.022222', '0.0314159', '0.0098765',
        '0.010203', '0.69696969', '0.42042042', '0.12312312',
        '0.1000001', '0.00010001',
    ]
    for v in vals:
        s = score_float(v)
        print(f'{s.float_value:>12}  tier={s.tier:<6} score={s.score:<3}  {"; ".join(s.reasons)}')


if __name__ == '__main__':
    main()