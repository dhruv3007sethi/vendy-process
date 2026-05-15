"""
bracket_parser.py
-----------------
Shared text-range bracket matching for tariff tables.

Handles these patterns (case-insensitive, commas stripped):
  "Under X"      / "Up to X" / "Until X"   →  value <= X
  "X to Y"       / "X - Y"                 →  X <= value <= Y
  "Over X"       / "Above X"               →  value > X
  "X and above"  / "X+"  / "X onwards"     →  value >= X
"""

import math
import re
from typing import Dict, List, Optional, Tuple


def parse_text_range(text: str) -> Tuple[float, float, bool]:
    """
    Parse a bracket range string into (lower, upper, is_exclusive_lower).

    is_exclusive_lower=True means value must be strictly > lower (used for "Over X").
    Otherwise the match is lower <= value <= upper.

    Returns (None, None, False) if the text cannot be parsed.
    """
    t = str(text).lower().replace(',', '').strip()

    # "X and above" / "X and over" — inclusive lower, no upper
    if 'and above' in t or 'and over' in t:
        num = _extract_number(t.split('and')[0])
        if num is not None:
            return (num, math.inf, False)

    # "Over X" / "Above X" — exclusive lower, no upper
    if t.startswith('over ') or t.startswith('above '):
        num = _extract_number(t)
        if num is not None:
            return (num, math.inf, True)

    # "X onwards" / "X onward" — inclusive lower, no upper
    if t.endswith('onwards') or t.endswith('onward'):
        num = _extract_number(t)
        if num is not None:
            return (num, math.inf, False)

    # "X+" suffix — inclusive lower, no upper
    if t.endswith('+'):
        num = _extract_number(t.replace('+', ''))
        if num is not None:
            return (num, math.inf, False)

    # "Under X" / "Up to X" / "Until X" — inclusive upper
    if t.startswith('under ') or t.startswith('up to ') or t.startswith('until '):
        num = _extract_number(t)
        if num is not None:
            return (0.0, num, False)

    # "X to Y"
    if ' to ' in t:
        parts = t.split(' to ')
        if len(parts) == 2:
            lo = _extract_number(parts[0])
            hi = _extract_number(parts[1])
            if lo is not None and hi is not None:
                return (lo, hi, False)

    # "X - Y" (dash-separated)
    if ' - ' in t:
        parts = t.split(' - ')
        if len(parts) == 2:
            lo = _extract_number(parts[0])
            hi = _extract_number(parts[1])
            if lo is not None and hi is not None:
                return (lo, hi, False)

    return (None, None, False)


def dimension_in_range(value: float, lower: float, upper: float,
                       is_exclusive_lower: bool) -> bool:
    """Check whether value falls within the parsed range."""
    if is_exclusive_lower:
        return value > lower
    return lower <= value <= upper


def match_bracket_row(tariff_matrix: List[Dict], dimension_value: float,
                      range_key: str = None) -> Optional[Dict]:
    """
    Iterate a tariff matrix and return the first row whose text range matches
    dimension_value.

    range_key: which dict key holds the range text. If None, tries common names.
    """
    _RANGE_KEYS = ('dimension_range', 'gt_range', 'grt_bracket',
                   'trb_range', 'bracket', 'loa_range')

    for row in tariff_matrix:
        text = _get_range_text(row, range_key, _RANGE_KEYS)
        if not text:
            continue

        lower, upper, exclusive = parse_text_range(text)
        if lower is None:
            continue

        if dimension_in_range(dimension_value, lower, upper, exclusive):
            return row

    return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _extract_number(text: str) -> Optional[float]:
    cleaned = re.sub(r'[^\d.]', '', text)
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _get_range_text(row: Dict, explicit_key: str, fallback_keys: tuple) -> str:
    if explicit_key and explicit_key in row:
        return str(row[explicit_key]).strip()
    for k in fallback_keys:
        v = row.get(k)
        if v:
            return str(v).strip()
    return ''
