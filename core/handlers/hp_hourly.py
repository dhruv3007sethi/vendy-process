
"""
hp_hourly.py
------------
Handles rate calculation for ports that bill by:
    Rate = rate_per_hp_hour × tug_HP × hours_per_tug

Port covered: Coatzacoalcos

Billing logic:
- GRT bracket determines the rate per HP per hour
- Each tug is billed separately based on its own HP
- Standard duration is 1 hour (included in base)
- Overtime: 25% of base per 15-minute increment beyond 1 hour
- Bow thruster discount: 10% if single tug with operational bow thruster
- Small vessel discount: 70% of minimum scale for vessels under 3,000 GRT
"""

import math
import re
import logging
from typing import List, Dict, Union, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# BRACKET LOOKUP FOR HP/HOUR RATE
# ---------------------------------------------------------------------------

def _get_rate_per_hp_hour(tariff_matrix: List[Dict], grt_value: float) -> float:
    """
    Finds the rate_per_hp_hour_usd from the GRT bracket table.
    Handles text bracket formats: "Up to 5,000", "5,001 to 10,000",
    "25,001 and above", "Over 25,000".

    Returns:
        float: rate per HP per hour
    """
    for row in tariff_matrix:
        bracket_text = str(
            row.get('dimension_range') or
            row.get('grt_bracket') or row.get('bracket') or row.get('trb_range') or ''
        ).lower().replace(',', '').strip()

        rate = row.get('rate_per_hp_hour_usd') or row.get('rate_per_hp_hour')
        if rate is None:
            continue

        # 1. "Up to X" (Inclusive)
        if bracket_text.startswith('up to ') or bracket_text.startswith('under '):
            limit = float(re.sub(r'[^\d.]', '', bracket_text))
            if grt_value <= limit:
                return float(rate)

        # 2. "X and above" (Inclusive)
        elif 'and above' in bracket_text or bracket_text.endswith('+'):
            limit = float(re.sub(r'[^\d.]', '', bracket_text.replace('and above', '').replace('+', '').strip()))
            if grt_value >= limit:
                return float(rate)

        # 3. "Over X" (Exclusive)
        elif bracket_text.startswith('over ') or bracket_text.startswith('above '):
            limit = float(re.sub(r'[^\d.]', '', bracket_text))
            if grt_value > limit:
                return float(rate)

        # 4. "X to Y" (Inclusive)
        elif ' to ' in bracket_text:
            parts = bracket_text.split(' to ')
            try:
                lower = float(re.sub(r'[^\d.]', '', parts[0]))
                upper = float(re.sub(r'[^\d.]', '', parts[1]))
                if lower <= grt_value <= upper:
                    return float(rate)
            except (ValueError, IndexError):
                continue

    raise ValueError(
        f"No GRT bracket match found for GRT={grt_value} in hp_hourly tariff matrix."
    )


# ---------------------------------------------------------------------------
# OVERTIME CALCULATION
# ---------------------------------------------------------------------------

def _calculate_overtime(
    base_rate_per_tug: float,
    actual_duration_hrs: float,
    standard_duration_hrs: float = 1.0,
    increment_mins: int = 15,
    surcharge_pct_per_increment: float = 0.25
) -> float:
    """
    Calculates overtime charge per tug.
    Each increment (default 15 mins) beyond standard duration
    is charged at surcharge_pct_per_increment × base_rate_per_tug.
    Any fraction of an increment rounds up to a full increment.

    Returns:
        float: overtime charge per tug
    """
    if actual_duration_hrs <= standard_duration_hrs:
        return 0.0

    excess_hrs = actual_duration_hrs - standard_duration_hrs
    excess_mins = excess_hrs * 60
    # Ceiling division to ensure partial increments are charged fully
    increments = math.ceil(excess_mins / increment_mins)
    return increments * surcharge_pct_per_increment * base_rate_per_tug


# ---------------------------------------------------------------------------
# MAIN PUBLIC FUNCTION
# ---------------------------------------------------------------------------

def calculate_hp_hourly(
    grt_value: float,
    tug_hp_list: List[float],
    actual_duration_hrs: float,
    tariff_matrix: List[Dict],
    standard_duration_hrs: float = 1.0,
    bow_thruster_discount: bool = False,
    small_vessel_discount: bool = False,
    small_vessel_threshold_grt: float = 3000.0,
    small_vessel_multiplier: float = 0.70
) -> Dict[str, Union[float, str, List[Dict]]]:
    """
    Calculates total hp_hourly charge across all tugs.

    Args:
        grt_value              : Vessel GRT (determines rate per HP/hour bracket)
        tug_hp_list            : List of HP values per tug (from invoice)
                                 e.g. [3200, 3200] for two tugs of 3200 HP each
        actual_duration_hrs    : Actual service duration in hours (from SOF)
        tariff_matrix          : The tariff_matrix_per_tug list from the port JSON
        standard_duration_hrs  : Duration included in base rate (default 1 hr)
        bow_thruster_discount  : True if single tug + operational bow thruster
        small_vessel_discount  : True if vessel is under small_vessel_threshold_grt
        small_vessel_threshold_grt : GRT cutoff for small vessel discount
        small_vessel_multiplier    : Rate multiplier for small vessels (default 0.70)

    Returns:
        dict with keys:
            total_charge          : float  — total charge across all tugs
            rate_per_hp_hour      : float  — effective rate used (after discounts)
            base_rate_per_hp_hour : float  — raw rate from tariff (before discounts)
            tug_breakdown         : list   — per-tug charge detail
            overtime_total        : float  — total overtime across all tugs
            discount_applied      : str    — description of discount if any
            grt_used              : float
            duration_hrs          : float
    """
    if not tug_hp_list:
        raise ValueError("tug_hp_list cannot be empty.")

    # 1. Determine Base Rate
    base_rate_per_hp_hour = _get_rate_per_hp_hour(tariff_matrix, grt_value)

    # 2. Apply Discounts
    discount_applied = "none"
    effective_rate = base_rate_per_hp_hour

    # Priority: Small vessel discount (usually mutually exclusive or takes precedence)
    # Logic: 70% of the rate.
    if small_vessel_discount and grt_value < small_vessel_threshold_grt:
        effective_rate = base_rate_per_hp_hour * small_vessel_multiplier
        discount_applied = f"small_vessel_{int(small_vessel_multiplier * 100)}pct"
        logger.info(f"Applied small vessel discount to GRT {grt_value}")

    # Bow Thruster Discount: 10% if single tug
    # Assumption: If small vessel discount applies, we might not apply BT discount, 
    # or we apply it on top. Assuming they are mutually exclusive or BT overrides 
    # if explicitly requested, but usually BT is for larger vessels.
    # Implementation: Check if not already discounted by small vessel rule, or override.
    elif bow_thruster_discount and len(tug_hp_list) == 1:
        effective_rate = base_rate_per_hp_hour * 0.90
        discount_applied = "bow_thruster_10pct"
        logger.info(f"Applied bow thruster discount for single tug.")

    # 3. Calculate Per-Tug Costs
    tug_breakdown = []
    total_charge = 0.0
    overtime_total = 0.0

    for i, hp in enumerate(tug_hp_list):
        # Base charge: Rate * HP * Standard Duration
        base_charge_this_tug = effective_rate * hp * standard_duration_hrs
        
        # Overtime: calculated on the Base Charge of this tug
        overtime_this_tug = _calculate_overtime(
            base_rate_per_tug=base_charge_this_tug,
            actual_duration_hrs=actual_duration_hrs,
            standard_duration_hrs=standard_duration_hrs
        )
        
        tug_total = base_charge_this_tug + overtime_this_tug
        overtime_total += overtime_this_tug
        total_charge += tug_total

        tug_breakdown.append({
            'tug_index': i + 1,
            'tug_hp': hp,
            'base_charge': round(base_charge_this_tug, 2),
            'overtime': round(overtime_this_tug, 2),
            'tug_total': round(tug_total, 2)
        })

    return {
        'total_charge': round(total_charge, 2),
        'rate_per_hp_hour': effective_rate,
        'base_rate_per_hp_hour': base_rate_per_hp_hour, # Raw rate for reference
        'tug_breakdown': tug_breakdown,
        'overtime_total': round(overtime_total, 2),
        'discount_applied': discount_applied,
        'grt_used': grt_value,
        'duration_hrs': actual_duration_hrs
    }


# ─────────────────────────────────────────────────────────────────────
# SELF TEST
# ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # Mock Tariff Matrix
    tariff_matrix = [
        {"grt_bracket": "Up to 5,000", "rate_per_hp_hour_usd": 0.15},
        {"grt_bracket": "5,001 to 10,000", "rate_per_hp_hour_usd": 0.18},
        {"grt_bracket": "Over 10,000", "rate_per_hp_hour_usd": 0.20},
    ]

    print("--- Test 1: Standard Service (1 Hour) ---")
    # 2 Tugs, 3200 HP each. GRT 4,000 (Rate 0.15). Duration 1.0 hr.
    res = calculate_hp_hourly(
        grt_value=4000, 
        tug_hp_list=[3200, 3200], 
        actual_duration_hrs=1.0, 
        tariff_matrix=tariff_matrix
    )
    # Expected Base: 0.15 * 3200 * 1 = 480. Total = 960.
    print(f"Total: ${res['total_charge']} (Breakdown: {res['tug_breakdown']})")

    print("\n--- Test 2: Overtime (1 Hour 20 Mins) ---")
    # Duration 1.33 hrs. Excess 20 mins = 2 increments (15 min each).
    # Overtime per tug = 2 * 0.25 * 480 = 240.
    # Total per tug = 480 + 240 = 720. Grand Total = 1440.
    res = calculate_hp_hourly(
        grt_value=4000, 
        tug_hp_list=[3200, 3200], 
        actual_duration_hrs=1.33, 
        tariff_matrix=tariff_matrix
    )
    print(f"Total: ${res['total_charge']} (Overtime Total: ${res['overtime_total']})")

    print("\n--- Test 3: Small Vessel Discount ---")
    # GRT 2500. Rate 0.15. 70% multiplier -> Effective 0.105.
    # Base: 0.105 * 3200 * 1 = 336.
    res = calculate_hp_hourly(
        grt_value=2500, 
        tug_hp_list=[3200], 
        actual_duration_hrs=1.0, 
        tariff_matrix=tariff_matrix, 
        small_vessel_discount=True
    )
    print(f"Total: ${res['total_charge']} (Rate: ${res['rate_per_hp_hour']})")

    print("\n--- Test 4: Bow Thruster Discount ---")
    # Single tug. GRT 12000 (Rate 0.20). 10% multiplier -> Effective 0.18.
    # Base: 0.18 * 3200 * 1 = 576.
    res = calculate_hp_hourly(
        grt_value=12000, 
        tug_hp_list=[3200], 
        actual_duration_hrs=1.0, 
        tariff_matrix=tariff_matrix, 
        bow_thruster_discount=True
    )
    print(f"Total: ${res['total_charge']} (Discount: {res['discount_applied']})")
