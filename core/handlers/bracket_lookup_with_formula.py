"""
bracket_lookup_with_formula.py
-------------------------------
Handles rate calculation for ports that use a bracket table up to a GT
threshold, then switch to a formula above that threshold.

Port covered: Ceuta

Structure per tariff table (T-2, T-1, T0, T+1, T+2):
  - GT up to 10,000  : fixed rate looked up from bracket table
  - GT above 10,000  : formula — base_rate + multiplier * [(GT/1000) - 10] * 100

Tariff table selection:
  - Accepted on face value from invoice (do not attempt to derive from SOF)
  - Tables reflect vendor's accumulated GT tier from prior year

Zone:
  - zone_i_interior : no surcharge
  - bay_area        : 25% surcharge on base rate

Ancillary rates per table:
  - displacement_fee : fixed fee if second tug travels from another port
  - hourly_rate      : billed per hour or fraction for retention/interruption
  - retention_rate   : billed per hour or fraction during service interruption
"""

import re
import logging
from typing import Optional, Dict, List, Union

from core.bracket_parser import match_bracket_row

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# GT BRACKET PARSER (up to threshold)
# ---------------------------------------------------------------------------

def _match_gt_bracket(fixed_rates: List[Dict], gt_value: float) -> Optional[float]:
    row = match_bracket_row(fixed_rates, gt_value, range_key='gt_range')
    if row is None:
        return None
    rate = row.get('rate_per_service')
    return float(rate) if rate is not None else None


# ---------------------------------------------------------------------------
# FORMULA PARSER (above threshold)
# ---------------------------------------------------------------------------

def _parse_and_apply_formula(formula_str: str, gt_value: float) -> float:
    """
    Parses and evaluates the Ceuta above-threshold formula.

    Expected format: "BASE + MULTIPLIER * [(GT / 1000) - 10] * 100"
    Example: "1143.38 + 1.1047 * [(GT / 1000) - 10] * 100"

    Args:
        formula_str : The formula string from the tariff JSON
        gt_value    : Vessel GT

    Returns:
        float: Calculated rate
    """
    # Extract base value and multiplier using regex.
    # Pattern matches: BASE + MULT * [ ( GT / 1000 ) - THRESHOLD ] * 100
    # We make the inner parenthesis optional to handle variations in formatting.
    pattern = r'([\d.]+)\s*\+\s*([\d.]+)\s*\*\s*\[\s*\(?(\s*GT\s*/\s*1000\s*)\)?\s*-\s*([\d.]+)\s*\]'
    match = re.search(pattern, formula_str, re.IGNORECASE)

    if match:
        base = float(match.group(1))
        multiplier = float(match.group(2))
        # Group 3 contains the division part, ignored in calculation logic as it's standard 1000
        threshold_units = float(match.group(4))  # usually 10 (= 10,000 GT / 1000)

        # Formula: Base + Mult * ( (GT/1000) - 10 ) * 100
        excess_units = (gt_value / 1000) - threshold_units
        rate = base + (multiplier * excess_units * 100)
        return round(rate, 2)

    # Fallback: try simplified form "(multiplier * GT) + constant"
    # e.g. "(0.11334 * GT) + 39.67"
    simplified = re.search(r'\(([\d.]+)\s*\*\s*GT\)\s*\+\s*([\d.]+)', formula_str, re.IGNORECASE)
    if simplified:
        multiplier = float(simplified.group(1))
        constant = float(simplified.group(2))
        return round(multiplier * gt_value + constant, 2)

    raise ValueError(
        f"Cannot parse formula: '{formula_str}'. "
        "Expected format: 'BASE + MULT * [(GT / 1000) - THRESHOLD] * 100'"
    )


# ---------------------------------------------------------------------------
# TABLE SELECTOR
# ---------------------------------------------------------------------------

def _get_table(tariff_tables: List[Dict], table_id: str) -> Optional[Dict]:
    """
    Retrieves the tariff table dict matching the given table_id.
    table_id values: "T-2", "T-1", "T0", "T+1", "T+2"
    """
    for table in tariff_tables:
        if str(table.get('table_id', '')).strip() == table_id.strip():
            return table
    return None


# ---------------------------------------------------------------------------
# MAIN PUBLIC FUNCTION
# ---------------------------------------------------------------------------

def calculate_bracket_with_formula(
    gt_value: float,
    table_id: str,
    tariff_tables: List[Dict],
    zone: str = "zone_i_interior",
    gt_threshold: float = 10000.0
) -> Dict[str, Union[float, str, bool, Dict]]:
    """
    Calculates the base service charge for Ceuta.

    For GT <= threshold: rate from fixed bracket table.
    For GT > threshold : rate from formula embedded in the table.

    Args:
        gt_value       : Vessel Gross Tonnage
        table_id       : Tariff table from invoice face value
                         ("T-2", "T-1", "T0", "T+1", "T+2")
        tariff_tables  : List of tariff table dicts from port JSON
        zone           : "zone_i_interior" (default) or "bay_area"
                         Bay area adds 25% surcharge on base rate
        gt_threshold   : GT above which formula applies (default 10,000)

    Returns:
        dict with keys:
            base_rate          : float — rate before zone surcharge
            zone_surcharge     : float — additional amount from bay area (0 if zone_i)
            total_rate         : float — base_rate + zone_surcharge
            calculation_method : str   — "bracket_lookup" or "formula"
            table_id_used      : str
            zone_applied       : str
            gt_used            : float
            ancillary_rates    : dict  — displacement_fee, hourly_rate, retention_rate
    """
    table = _get_table(tariff_tables, table_id)

    if table is None:
        available = [t.get('table_id') for t in tariff_tables]
        raise ValueError(
            f"Table '{table_id}' not found. Available tables: {available}"
        )

    # --- Select calculation method ---
    if gt_value <= gt_threshold:
        fixed_rates = table.get('fixed_rates', [])
        if not fixed_rates:
            raise ValueError(f"'fixed_rates' list missing in table '{table_id}'")
            
        base_rate = _match_gt_bracket(fixed_rates, gt_value)

        if base_rate is None:
            # Provide details for debugging
            ranges = [r.get('gt_range') for r in fixed_rates]
            raise ValueError(
                f"No bracket match for GT={gt_value} in table '{table_id}'. "
                f"Brackets available: {ranges}"
            )
        calculation_method = "bracket_lookup"

    else:
        formula_str = table.get('formula_over_10000_gt')
        if not formula_str:
            raise ValueError(
                f"No formula found in table '{table_id}' for GT above {gt_threshold}."
            )
        logger.debug(f"Applying formula for GT {gt_value}: {formula_str}")
        base_rate = _parse_and_apply_formula(formula_str, gt_value)
        calculation_method = "formula"

    # --- Zone surcharge ---
    # Normalize zone string to handle case variations
    zone_normalized = zone.strip().lower()
    zone_applied = zone  # Keep original case for output
    zone_surcharge = 0.0
    
    if "bay" in zone_normalized:
        zone_surcharge = round(base_rate * 0.25, 2)
        zone_applied = "bay_area"
    else:
        zone_applied = "zone_i_interior"

    total_rate = round(base_rate + zone_surcharge, 2)

    # --- Ancillary rates (for reference by output formatter) ---
    ancillary_raw = table.get('ancillary_rates', {})
    ancillary_rates = {
        'displacement_fee': float(ancillary_raw.get('displacement_fee', 0.0)),
        'hourly_rate': float(ancillary_raw.get('hourly_rate', 0.0)),
        'retention_rate': float(ancillary_raw.get('retention_rate', 0.0))
    }

    return {
        'base_rate': round(base_rate, 2),
        'zone_surcharge': zone_surcharge,
        'total_rate': total_rate,
        'calculation_method': calculation_method,
        'table_id_used': table_id,
        'zone_applied': zone_applied,
        'gt_used': gt_value,
        'ancillary_rates': ancillary_rates
    }


# ─────────────────────────────────────────────────────────────────────
# SELF TEST
# ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # Mock Tariff Data
    tariff_tables = [
        {
            "table_id": "T+2",
            "fixed_rates": [
                {"gt_range": "0 to 2,000", "rate_per_service": 278.04},
                {"gt_range": "2,001 to 4,500", "rate_per_service": 556.09},
                {"gt_range": "7,001 to 10,000", "rate_per_service": 1112.18}
            ],
            # Formula: 1112.18 + 1.0746 * [ (GT / 1000) - 10 ] * 100
            "formula_over_10000_gt": "1112.18 + 1.0746 * [(GT / 1000) - 10] * 100",
            "ancillary_rates": {
                "displacement_fee": 1766.30,
                "hourly_rate": 373.91,
                "retention_rate": 450.00
            }
        }
    ]

    print("--- Test 1: Bracket Lookup (GT 4,500) ---")
    res = calculate_bracket_with_formula(4500, "T+2", tariff_tables)
    print(f"Total Rate: ${res['total_rate']} (Method: {res['calculation_method']})")
    # Expected: 556.09

    print("\n--- Test 2: Formula (GT 23,403) ---")
    # Calc: 1112.18 + 1.0746 * ((23.403 - 10) * 100) = 1112.18 + 1440.44 = 2552.62
    res = calculate_bracket_with_formula(23403, "T+2", tariff_tables)
    print(f"Total Rate: ${res['total_rate']} (Base: ${res['base_rate']})")
    
    print("\n--- Test 3: Bay Area Surcharge (GT 4,500) ---")
    # Base 556.09. Surcharge 139.02. Total 695.11.
    res = calculate_bracket_with_formula(4500, "T+2", tariff_tables, zone="Bay Area")
    print(f"Total Rate: ${res['total_rate']} (Surcharge: ${res['zone_surcharge']})")
    
    print("\n--- Test 4: Ancillary Rates ---")
    print(f"Displacement Fee: ${res['ancillary_rates']['displacement_fee']}")

    