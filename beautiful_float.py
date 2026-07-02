#!/usr/bin/env python3
"""Beautiful / rare CS2 float scorer.

Scores float values by collector aesthetics:
- repeated digits: 0.00666666, 0.0007777
- many leading zeros after decimal
- meme numbers: 1337, 420, 69, 666, 777, 555, 888
- palindromes / sequences
- ultra-low practical rarity

This is NOT a price guarantee; it is a signal generator for manual review.
"""
from __future__ import annotations

import argparse
import re
from dataclasses import dataclass, asdict
from decimal import Decimal, InvalidOperation

MEME_PATTERNS = {
    '1337': 18,
    '420': 10,
    '0420': 14,
    '069': 10,
    '0069': 14,
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
}

@dataclass
class FloatScore:
    float_value: str
    score: int
    tier: str
    reasons: list[str]
    decimal_digits: str


def normalize_float(value: str | float) -> tuple[str, str]:
    """Return display value and decimal digits without leading '0.'."""
    s = str(value).strip()
    try:
        d = Decimal(s)
    except InvalidOperation:
        raise ValueError(f'Invalid float: {value}')
    if d < 0 or d > 1:
        raise ValueError(f'Float must be between 0 and 1: {value}')
    # Keep enough decimals; avoid scientific notation.
    fixed = format(d, 'f')
    if '.' not in fixed:
        fixed += '.0'
    int_part, dec = fixed.split('.', 1)
    dec = dec.rstrip('0') or '0'
    return f'{int_part}.{dec}', dec


def longest_run(s: str) -> tuple[str, int, int]:
    best_digit=''; best_len=0; best_pos=0
    i=0
    while i < len(s):
        j=i+1
        while j < len(s) and s[j] == s[i]:
            j += 1
        run_len = j-i
        if run_len > best_len:
            best_digit=s[i]; best_len=run_len; best_pos=i
        i=j
    return best_digit, best_len, best_pos


def leading_zeros(dec: str) -> int:
    return len(dec) - len(dec.lstrip('0'))


def is_palindrome_chunk(dec: str, min_len: int = 5) -> str | None:
    # Check first 5-8 meaningful digits and any window.
    for n in range(min(8, len(dec)), min_len-1, -1):
        chunk = dec[:n]
        if chunk == chunk[::-1]:
            return chunk
    for n in range(min(8, len(dec)), min_len-1, -1):
        for i in range(0, len(dec)-n+1):
            chunk = dec[i:i+n]
            if chunk == chunk[::-1]:
                return chunk
    return None


def sequence_chunk(dec: str) -> str | None:
    sequences = ['0123456789', '123456789', '9876543210', '987654321']
    for seq in sequences:
        for n in range(min(len(seq), 7), 4, -1):
            for i in range(0, len(seq)-n+1):
                chunk = seq[i:i+n]
                if chunk in dec:
                    return chunk
    return None


def score_float(value: str | float) -> FloatScore:
    display, dec = normalize_float(value)
    reasons=[]
    score=0

    lz = leading_zeros(dec)
    if lz >= 5:
        add = 35 + (lz-5)*8
        score += add
        reasons.append(f'ultra low: {lz} leading zeros (+{add})')
    elif lz == 4:
        score += 25; reasons.append('very low: 4 leading zeros (+25)')
    elif lz == 3:
        score += 15; reasons.append('low: 3 leading zeros (+15)')
    elif lz == 2:
        score += 8; reasons.append('2 leading zeros (+8)')

    digit, run_len, pos = longest_run(dec)
    if run_len >= 6:
        add = 35 + (run_len-6)*8
        score += add
        reasons.append(f'{run_len}x repeated digit {digit} (+{add})')
    elif run_len == 5:
        score += 25; reasons.append(f'5x repeated digit {digit} (+25)')
    elif run_len == 4:
        score += 14; reasons.append(f'4x repeated digit {digit} (+14)')
    elif run_len == 3:
        score += 6; reasons.append(f'3x repeated digit {digit} (+6)')

    # Bonus if repetition starts early after leading zeros, e.g. 0.00666666.
    if run_len >= 4 and pos <= 3:
        score += 8
        reasons.append('main repeat appears early (+8)')

    for pat, pts in MEME_PATTERNS.items():
        if pat in dec:
            score += pts
            reasons.append(f'meme pattern {pat} (+{pts})')
            # Avoid stacking too many tiny meme patterns.
            if pts >= 14:
                break

    pal = is_palindrome_chunk(dec)
    if pal:
        score += 12
        reasons.append(f'palindrome {pal} (+12)')

    seq = sequence_chunk(dec)
    if seq:
        score += 14
        reasons.append(f'sequence {seq} (+14)')

    # All same first significant digits, e.g. 0.000111, 0.000222.
    stripped = dec.lstrip('0')
    if len(stripped) >= 4 and len(set(stripped[:4])) == 1:
        score += 10
        reasons.append(f'first significant digits repeat {stripped[:4]} (+10)')

    if score >= 80:
        tier='S'
    elif score >= 55:
        tier='A'
    elif score >= 35:
        tier='B'
    elif score >= 18:
        tier='C'
    else:
        tier='normal'

    if not reasons:
        reasons.append('no collector-number pattern')
    return FloatScore(display, score, tier, reasons, dec)


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('floats', nargs='*')
    args=ap.parse_args()
    vals=args.floats or ['0.00666666','0.0007777','0.001337','0.000420','0.000069','0.012345','0.054321','0.00555555','0.022222','0.0314159','0.0098765','0.010203']
    for v in vals:
        s=score_float(v)
        print(f'{s.float_value:>12}  tier={s.tier:<6} score={s.score:<3}  {"; ".join(s.reasons)}')

if __name__ == '__main__':
    main()
