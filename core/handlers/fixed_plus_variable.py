"""
fixed_plus_variable.py
----------------------
Handles rate calculation for ports using the formula:
    Rate = Fixed_Part + (Variable_Part × Vessel_GT)

Ports covered:
    Algeciras, Valencia, Las Palmas, Cadiz Bay, Huelva, Tenerife/La Palma

Rate source types:
    Type A — Rates embedded directly in calculation profile (Algeciras)
    Type B — Rates in a GT bracket table, each bracket has fixed + variable
              values, optionally split by zone/terminal column
              (Valencia, Tenerife, Huelva, Las Palmas, Cadiz Bay)
"""

import re
import logging
from typing import Optional, List, Dict, Union

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# BRACKET PARSING FOR FIXED + VARIABLE
# ---------------------------------------------------------------------------

def _parse_gt_range_text(gt_range_text: str) -> tuple[Optional[float], Optional[float], bool]:
    """
    Converts GT range text to (lower, upper, is_exclusive_lower) numeric bounds.
    
    Handles:
      "0 to 1,000"        -> (0, 1000, False)
      "1,001 to 5,000"    -> (1001, 5000, False)
      "Over 100,000"      -> (100000, inf, True)  # GT must be > 100,000
      "100,000 and above"-> (100000, inf, False) # GT must be >= 100,000
      "75,001 to 100,000" -> (75001, 100000, False)
      
    Returns:
        (lower, upper, is_exclusive_lower): 
            If is_exclusive_lower is True, the match is `gt > lower`.
            Otherwise, match is `lower <= gt <= upper`.
    """
    text = str(gt_range_text).lower().replace(',', '').strip()

    # Handle "X and above" (Inclusive)
    if 'and above' in text or 'and over' in text:
        lower = float(re.sub(r'[^\d.]', '', text.split('and')[0]))
        return (lower, float('inf'), False)

    # Handle "Over X" or "Above X" (Exclusive)
    if text.startswith('over ') or text.startswith('above '):
        lower = float(re.sub(r'[^\d.]', '', text))
        return (lower, float('inf'), True)

    # Handle "X to Y"
    if ' to ' in text:
        parts = text.split(' to ')
        if len(parts) == 2:
            lower = float(re.sub(r'[^\d.]', '', parts[0]))
            upper = float(re.sub(r'[^\d.]', '', parts[1]))
            return (lower, upper, False)

    # Handle "X - Y" (Valencia/Spanish format, e.g. "22,001 - 30,000")
    if ' - ' in text:
        parts = text.split(' - ')
        if len(parts) == 2:
            lower = float(re.sub(r'[^\d.]', '', parts[0]))
            upper = float(re.sub(r'[^\d.]', '', parts[1]))
            return (lower, upper, False)

    # Handle "X onwards" / "X onward" (e.g. "100,000 onwards") → same as "and above"
    if text.endswith('onwards') or text.endswith('onward'):
        lower = float(re.sub(r'[^\d.]', '', text))
        return (lower, float('inf'), False)

    # Handle "Up to X" (Inclusive)
    if text.startswith('up to ') or text.startswith('until '):
        upper = float(re.sub(r'[^\d.]', '', text.replace('up to', '').replace('until', '')))
        return (0.0, upper, False)

    # Fallback: If we can't parse, return None to signal failure
    return (None, None, False)


def _find_bracket_row(tariff_matrix: List[Dict], gt_value: float) -> Optional[Dict]:
    """
    Finds the matching row in a GT bracket table.
    Supports two formats:
      - 'gt_range' text key  (e.g. "22,001 - 30,000", "Over 100,000")
      - 'up_to_gt' numeric key (e.g. Las Palmas: first row where up_to_gt >= gt_value)
    Returns the full row dict, or None if no match.
    """
    # Handle up_to_gt / dimension_max numeric style (e.g. Las Palmas)
    if tariff_matrix and ('up_to_gt' in tariff_matrix[0] or 'dimension_max' in tariff_matrix[0]):
        upper_key = 'dimension_max' if 'dimension_max' in tariff_matrix[0] else 'up_to_gt'
        sorted_rows = sorted(tariff_matrix, key=lambda r: r.get(upper_key, float('inf')))
        for row in sorted_rows:
            if gt_value <= row[upper_key]:
                return row
        return None

    for row in tariff_matrix:
        gt_range_text = row.get('dimension_range') or row.get('gt_range', '')
        if not gt_range_text:
            continue
        
        try:
            lower, upper, is_exclusive = _parse_gt_range_text(gt_range_text)
            
            if lower is None:
                continue

            # Determine match logic
            if is_exclusive:
                # e.g., "Over 100,000" -> GT must be > 100,000
                if gt_value > lower:
                    return row
            else:
                # e.g., "0 to 100,000" -> GT must be >= lower and <= upper
                if lower <= gt_value <= upper:
                    return row
        except (ValueError, TypeError):
            logger.warning(f"Skipping unparsable GT range: {gt_range_text}")
            continue
            
    return None


# ---------------------------------------------------------------------------
# RATE EXTRACTION FROM BRACKET ROW
# ---------------------------------------------------------------------------

def _extract_fixed_variable(row: Dict, zone_key: Optional[str] = None) -> tuple[float, float]:
    """
    Extracts (fixed, variable) from a matched bracket row.

    If zone_key is provided (e.g. "comercial", "general_zone"), looks for
    a nested dict with 'fixed' and 'variable' keys at that key.

    If no zone_key, looks for top-level 'fixed_rate' / 'variable_rate'
    or 'fixed' / 'variable' keys directly.

    Returns:
        (fixed_part, variable_part_per_gt)
    """
    # 1. Try Zone Specific
    if zone_key and zone_key in row:
        zone_data = row[zone_key]
        if isinstance(zone_data, dict):
            fixed = zone_data.get('fixed') or zone_data.get('fixed_rate') or 0.0
            variable = zone_data.get('variable') or zone_data.get('variable_rate') or 0.0
            return float(fixed), float(variable)
        else:
            logger.warning(f"Zone key '{zone_key}' found in row but is not a dict. Falling back to default.")

    # 2. Fallback: look for direct fixed/variable keys
    fixed = 0.0
    variable = 0.0
    found_fixed = False
    found_var = False

    for fixed_key in ['fixed_rate_eur', 'fixed_rate', 'fixed', 'fp', 'fixed_part',
                      'fixed_amount_if', 'if']:
        if fixed_key in row:
            fixed = float(row[fixed_key])
            found_fixed = True
            break

    for var_key in ['variable_rate_per_gt_eur', 'variable_rate_per_gt',
                    'variable_rate', 'variable', 'vp', 'variable_part',
                    'variable_amount_iv', 'iv']:
        if var_key in row:
            variable = float(row[var_key])
            found_var = True
            break

    if not found_fixed or not found_var:
        raise ValueError(
            f"Cannot find fixed or variable rate in row. "
            f"Available keys: {list(row.keys())}. Requested Zone: {zone_key}"
        )

    return fixed, variable


# ---------------------------------------------------------------------------
# MAIN PUBLIC FUNCTION
# ---------------------------------------------------------------------------

def calculate_fixed_plus_variable(
    gt_value: float,
    rates_source: Union[Dict, List[Dict]],
    zone_key: Optional[str] = None
) -> Dict[str, any]:
    """
    Calculates the base service charge using: Rate = Fixed + (Variable × GT)

    Args:
        gt_value     : Vessel Gross Tonnage
        rates_source : One of:
                         - dict with 'tariff_matrix' key (Type B: bracket table)
                         - dict with 'fixed_rate_eur' + 'variable_rate_per_gt_eur'
                           keys directly (Type A: profile-embedded rates)
                         - list of bracket rows (Type B direct list)
        zone_key     : Column name within bracket row for zone-specific rates
                       e.g. "comercial", "general", "specialized_zones_ABC"
                       Leave None if rates are not zone-split within each bracket

    Returns:
        dict with keys:
            base_rate       : float  — the calculated Rate = FP + (VP × GT)
            fixed_part      : float  — FP used
            variable_part   : float  — VP used (per GT)
            gt_used         : float  — GT value used in calculation
            zone_applied    : str    — zone_key used (or "default")
            rate_source     : str    — "bracket_table" or "profile_direct"
            bracket_desc    : str    — description of matched bracket (if matrix used)

    Raises:
        ValueError if no bracket matches or required rate fields are missing.
    """

    # --- Type A: rates directly in profile (e.g. Algeciras) ---
    if isinstance(rates_source, dict):
        has_direct_rates = (
            any(k in rates_source for k in ['fixed_rate_eur', 'fixed_rate', 'fixed']) and
            any(k in rates_source for k in ['variable_rate_per_gt_eur', 'variable_rate_per_gt', 'variable'])
        )

        # Only use direct rates if there is no tariff_matrix / rate_table overriding it
        if has_direct_rates and 'tariff_matrix' not in rates_source and 'rate_table' not in rates_source:
            fixed, variable = _extract_fixed_variable(rates_source, zone_key)
            base_rate = fixed + (variable * gt_value)
            return {
                'base_rate': round(base_rate, 2),
                'fixed_part': fixed,
                'variable_part': variable,
                'gt_used': gt_value,
                'zone_applied': zone_key or 'default',
                'rate_source': 'profile_direct',
                'bracket_desc': 'N/A'
            }

    # --- Type B: bracket table ---
    tariff_matrix = None
    if isinstance(rates_source, list):
        tariff_matrix = rates_source
    elif isinstance(rates_source, dict):
        matrix = rates_source.get("rate_table") or rates_source.get("tariff_matrix")
        if isinstance(matrix, list):
            tariff_matrix = matrix
        elif isinstance(matrix, dict):
            # Multiple vessel type sub-tables — caller should pre-select, but fall back to first
            first_table = next(iter(matrix.values()), {})
            tariff_matrix = first_table.get("rates", []) if isinstance(first_table, dict) else []
        else:
            tariff_matrix = rates_source.get("rates", [])

    if tariff_matrix:
        row = _find_bracket_row(tariff_matrix, gt_value)
        if row is None:
            raise ValueError(
                f"No GT bracket found for GT={gt_value}. "
                f"Table has {len(tariff_matrix)} rows."
            )
        fixed, variable = _extract_fixed_variable(row, zone_key)
        base_rate = fixed + (variable * gt_value)
        return {
            'base_rate': round(base_rate, 2),
            'fixed_part': fixed,
            'variable_part': variable,
            'gt_used': gt_value,
            'zone_applied': zone_key or 'default',
            'rate_source': 'bracket_table',
            'bracket_desc': row.get('gt_range', 'unknown')
        }

    raise ValueError(
        f"calculate_fixed_plus_variable: could not determine rate. "
        f"GT={gt_value}, rates_source type={type(rates_source).__name__}, "
        f"keys={list(rates_source.keys()) if isinstance(rates_source, dict) else 'N/A'}"
    )
