"""
core/bracket_lookup.py
-----------------
Handles rate lookup for ports that use bracket/range tables.
Covers LOA-based ports (Ghent, Le Havre, Antwerp, Rotterdam, Dordrecht/Moerdijk)
and GT-based ports (Santo Domingo, Rostock, Brake).

Bracket formats handled:
  1. String keys  : {"up_to_114.99": 1450, "115_to_129.99": 1675, ...}
  2. Dict objects : [{"min_gt": 0, "max_gt": 5000, "rate": 1200}, ...]
  3. Text ranges  : [{"grt_bracket": "Under 2,500", "rate": 20833}, ...]
"""

import re
import math
import logging
from typing import Union, List

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# BRACKET PARSING UTILITIES
# ---------------------------------------------------------------------------

def _parse_string_key_brackets(rates: dict, dimension_value: float) -> float:
    """
    Parses bracket keys like:
      "up_to_114.99"     -> 0 to 114.99
      "115_to_129.99"    -> 115 to 129.99
      "up_to_138m"       -> 0 to 138
      "139m_150m"        -> 139 to 150
    Returns the matched rate or raises ValueError if no bracket matches.
    """
    for key, rate in rates.items():
        # Normalise: remove 'm' suffix, strip whitespace
        normalised = key.strip().lower().replace('m', '')

        if normalised.startswith('up_to_'):
            # e.g., "up_to_114.99" -> value <= 114.99
            upper = float(normalised.replace('up_to_', ''))
            if dimension_value <= upper:
                return float(rate)
        else:
            # Expect pattern: "low_to_high" or "low_high" after stripping
            parts = re.split(r'_to_|_', normalised)
            parts = [p for p in parts if p]  # remove empty strings
            if len(parts) == 2:
                try:
                    lower = float(parts[0])
                    upper = float(parts[1])
                    if lower <= dimension_value <= upper:
                        return float(rate)
                except ValueError:
                    # Ignore keys that can't be parsed to floats
                    continue

    raise ValueError(
        f"No bracket match found for dimension value {dimension_value} "
        f"in rates: {list(rates.keys())}"
    )


def _parse_dict_object_brackets(brackets: List[dict], dimension_value: float,
                                 rate_column: str = None) -> float:
    """
    Parses bracket lists like:
      [{"min_gt": 0, "max_gt": 5000, "rate": 1200}, ...]
    Accepts min/max_gt, min/max_loa, min/max_loa_m, min/max_grt naming.
    Assumes intervals are inclusive [min, max].
    rate_column: explicit rate field to read (e.g. "zandvliet_kallo_rate_eur" for Antwerp).
                 Falls back to generic "rate" / "rate_eur" keys.
    """
    for bracket in brackets:
        lower = bracket.get('dimension_min', 0)
        upper = bracket.get('dimension_max')

        if upper is None:
            continue

        if float(lower) <= dimension_value <= float(upper):
            if rate_column and rate_column in bracket:
                return float(bracket[rate_column])
            if 'base_rate' in bracket:
                return float(bracket['base_rate'])
            raise ValueError(
                f"Bracket matched for {dimension_value} but no rate field found. "
                f"Available keys: {list(bracket.keys())}. Specify rate_column."
            )

    raise ValueError(
        f"No bracket match found for dimension value {dimension_value} "
        f"in {len(brackets)} bracket objects."
    )


def _parse_text_range_brackets(brackets: List[dict], dimension_value: float,
                                rate_column: str = None) -> float:
    """
    Parses bracket lists like:
      [{"grt_bracket": "Under 2,500", "with_bow_thruster_rate": 20833}, ...]
    Text patterns handled:
      "Under X"      -> 0 to X (Exclusive)
      "X to Y"       -> X to Y (Inclusive)
      "Over X"       -> X to infinity (Exclusive)
      "X and above"  -> X to infinity (Inclusive)
      
    rate_column: which rate field to read (e.g. "with_bow_thruster_rate").
                 If None, tries common field names.
    """
    for bracket in brackets:
        text = str(bracket.get('dimension_range') or '')
        text_clean = text.lower().replace(',', '').strip()
        matched = False

        # Pattern: "Under X" (e.g., "Under 5,000")
        if text_clean.startswith('under '):
            upper = float(re.sub(r'[^\d.]', '', text_clean.replace('under', '')))
            matched = dimension_value <= upper

        # Pattern: "Up to X" (inclusive, e.g., "Up to 8,000 GT")
        elif text_clean.startswith('up to '):
            upper = float(re.sub(r'[^\d.]', '', text_clean.replace('up to', '')))
            matched = dimension_value <= upper

        # Pattern: "X to Y" (e.g., "5,000 to 10,000")
        elif ' to ' in text_clean:
            parts = text_clean.split(' to ')
            try:
                lower = float(re.sub(r'[^\d.]', '', parts[0]))
                upper = float(re.sub(r'[^\d.]', '', parts[1]))
                matched = lower <= dimension_value <= upper
            except ValueError:
                pass

        # Pattern: "X - Y" (dash format, e.g., "8,001 - 15,000 GT")
        elif ' - ' in text_clean:
            parts = text_clean.split(' - ')
            try:
                lower = float(re.sub(r'[^\d.]', '', parts[0]))
                upper = float(re.sub(r'[^\d.]', '', parts[1]))
                matched = lower <= dimension_value <= upper
            except ValueError:
                pass

        # Pattern: "Over X" (Strictly greater than, e.g., "Over 50,000")
        elif text_clean.startswith('over '):
            try:
                lower = float(re.sub(r'[^\d.]', '', text_clean.replace('over', '')))
                matched = dimension_value > lower
            except ValueError:
                pass

        # Pattern: "X and above" (Greater than or equal, e.g., "50,000 GT and above")
        elif 'and above' in text_clean:
            try:
                lower = float(re.sub(r'[^\d.]', '', text_clean.replace('and above', '')))
                matched = dimension_value >= lower
            except ValueError:
                pass

        if matched:
            if rate_column and rate_column in bracket:
                return float(bracket[rate_column])

            if 'base_rate' in bracket:
                return float(bracket['base_rate'])
            
            raise ValueError(
                f"Bracket matched for {dimension_value} ('{text}') but no rate field found. "
                f"Available fields: {list(bracket.keys())}. "
                f"Specify rate_column explicitly."
            )

    raise ValueError(
        f"No bracket match found for dimension value {dimension_value}."
    )


# ---------------------------------------------------------------------------
# MAIN PUBLIC FUNCTION
# ---------------------------------------------------------------------------

def lookup_bracket_rate(
    rates_data: Union[dict, list],
    dimension_value: float,
    rate_column: str = None
) -> float:
    """
    Auto-detects bracket format and returns the matched base rate.

    Args:
        rates_data     : The rates dict or list from the tariff JSON
        dimension_value: The vessel LOA / GT / GRT value
        rate_column    : For text-range brackets with multiple rate columns
                         (e.g. "with_bow_thruster_rate")

    Returns:
        float: The matched base rate (before surcharges or tug count multiplication)

    Raises:
        ValueError: If no bracket matches or format is unrecognised
    """
    if isinstance(rates_data, dict):
        # Format 1: string keys
        return _parse_string_key_brackets(rates_data, dimension_value)

    elif isinstance(rates_data, list):
        if len(rates_data) == 0:
            raise ValueError("Empty bracket list provided.")

        first = rates_data[0]

        # Format 2: dict objects with min/max keys
        if 'dimension_min' in first or 'dimension_max' in first:
            return _parse_dict_object_brackets(rates_data, dimension_value, rate_column)

        # Format 3: text range descriptions
        if 'dimension_range' in first:
            return _parse_text_range_brackets(rates_data, dimension_value, rate_column)

        raise ValueError(
            f"Unrecognised bracket list format. First item keys: {list(first.keys())}"
        )

    else:
        raise ValueError(
            f"rates_data must be a dict or list, got {type(rates_data)}"
        )


# ---------------------------------------------------------------------------
# OVERSIZE SUPPLEMENT (used by Rostock, Brake, and others)
# ---------------------------------------------------------------------------

def calculate_oversize_supplement(
    dimension_value: float,
    threshold: float,
    increment_unit: float,
    rate_per_increment: float
) -> float:
    """
    Calculates extra charge for vessels above an oversize threshold.
    Example (Rostock): 571 EUR per 10,000 GT above 50,000 GT.

    Args:
        dimension_value    : Vessel GT/GRT
        threshold          : GT above which oversize applies
        increment_unit     : Size of each billing increment (e.g. 10000)
        rate_per_increment : Charge per increment (e.g. 571)

    Returns:
        float: Additional oversize charge (0 if below threshold)
    """
    if dimension_value <= threshold:
        return 0.0

    excess = dimension_value - threshold
    # Any partial increment is charged as a full increment
    increments = math.ceil(excess / increment_unit)
    total = increments * rate_per_increment
    
    logger.debug(
        f"Oversize: Val {dimension_value} > Thresh {threshold}. "
        f"Excess {excess}, Increments {increments}, Charge {total}"
    )
    return total


# ---------------------------------------------------------------------------
# LARGE VESSEL INCREMENT (used by Ghent)
# ---------------------------------------------------------------------------

def calculate_large_vessel_increment(
    loa: float,
    apply_above_meters: float,
    increment_step_meters: float,
    surcharge_per_step: float
) -> float:
    """
    Calculates the large vessel increment for ports like Ghent.
    Example: +235 EUR per 15m above 265m LOA.

    Returns:
        float: Total large vessel increment charge (0 if below threshold)
    """
    if loa <= apply_above_meters:
        return 0.0

    excess = loa - apply_above_meters
    steps = math.ceil(excess / increment_step_meters)
    total = steps * surcharge_per_step
    
    logger.debug(
        f"Large Vessel Inc: LOA {loa} > Thresh {apply_above_meters}. "
        f"Excess {excess}, Steps {steps}, Charge {total}"
    )
    return total


# ─────────────────────────────────────────────────────────────────────
# SELF TEST
# ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    print("--- Test 1: String Keys ---")
    rates_str = {"up_to_100": 500, "101_to_200": 1000}
    print(f"50 GT  -> {lookup_bracket_rate(rates_str, 50)}")  # 500
    print(f"100 GT -> {lookup_bracket_rate(rates_str, 100)}") # 500
    print(f"150 GT -> {lookup_bracket_rate(rates_str, 150)}") # 1000

    print("\n--- Test 2: Dict Objects ---")
    rates_dict = [{"min_gt": 0, "max_gt": 5000, "rate": 200}]
    print(f"5000 GT -> {lookup_bracket_rate(rates_dict, 5000)}") # 200

    print("\n--- Test 3: Text Ranges (Inclusive/Exclusive Fix) ---")
    rates_text = [
        {"grt_bracket": "Under 2,500", "rate": 100},
        {"grt_bracket": "50,000 and above", "rate": 900},
        {"grt_bracket": "Over 60,000", "rate": 1000}
    ]
    print(f"2499 GRT -> {lookup_bracket_rate(rates_text, 2499)}") # 100
    print(f"2500 GRT -> {lookup_bracket_rate(rates_text, 2500)}") # Error (no match) or next bracket if existed
    print(f"50000 GRT -> {lookup_bracket_rate(rates_text, 50000)}") # 900 (Crucial fix)
    print(f"60000 GRT -> {lookup_bracket_rate(rates_text, 60000)}") # 900 (Not strictly > 60000)
    print(f"60001 GRT -> {lookup_bracket_rate(rates_text, 60001)}") # 1000

    print("\n--- Test 4: Oversize ---")
    # Over 50k, per 10k. Vessel 62,000. Excess 12k. 2 increments. 2 * 571 = 1142
    print(f"Rostock 62k GT -> {calculate_oversize_supplement(62000, 50000, 10000, 571)}")

    