"""
per_service_tug_count.py
------------------------
Handles rate calculation for ports where the total service rate is looked up
directly from a matrix of: TRB/GRT bracket × tug count × movement type.

Port covered: Ensenada

Key characteristics:
- Rate is per service (not per tug), already reflects tug count in the column
- Movement type: arrival or departure
- Some tug count / movement combinations are null (not permitted)
- Third tug (if requested) billed at the single-tug basic quota for the TRB range
- Overtime: 25% of base per 15-minute increment beyond 1 hour
- Bow thruster discount: 10% for cargo/container ships using one tug with bow thruster
- Small vessel discount: 70% for vessels under 2,500 TRB
"""

import math
import re
import logging
from typing import Optional, Dict, List, Union

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TRB BRACKET PARSER
# ---------------------------------------------------------------------------

def _match_trb_bracket(tariff_matrix: List[Dict], trb_value: float) -> Optional[Dict]:
    """
    Finds the matching row from a TRB/GRT range matrix.
    Handles: "Under 2,500", "2,501 to 5,000", "Over 10,000", "10,000 and above"
    Returns the full row dict or None.
    """
    for row in tariff_matrix:
        text = str(
            row.get('dimension_range') or
            row.get('trb_range') or row.get('grt_range') or row.get('bracket') or ''
        ).lower().replace(',', '').strip()

        if not text:
            continue

        # 1. "Under X" (Exclusive)
        if text.startswith('under '):
            upper = float(re.sub(r'[^\d.]', '', text))
            if trb_value < upper:
                return row
        
        # 2. "Up to X" (Inclusive)
        elif text.startswith('up to ') or text.startswith('until '):
            upper = float(re.sub(r'[^\d.]', '', text.replace('up to', '').replace('until', '')))
            if trb_value <= upper:
                return row

        # 3. "X and above" (Inclusive)
        elif 'and above' in text or text.endswith('+'):
            lower = float(re.sub(r'[^\d.]', '', text.replace('and above', '').replace('+', '').strip()))
            if trb_value >= lower:
                return row
        
        # 4. "Over X" (Exclusive)
        elif text.startswith('over ') or text.startswith('above '):
            lower = float(re.sub(r'[^\d.]', '', text))
            if trb_value > lower:
                return row

        # 5. "X to Y" (Inclusive)
        elif ' to ' in text:
            parts = text.split(' to ')
            try:
                lower = float(re.sub(r'[^\d.]', '', parts[0]))
                upper = float(re.sub(r'[^\d.]', '', parts[1]))
                if lower <= trb_value <= upper:
                    return row
            except (ValueError, IndexError):
                continue

    return None


# ---------------------------------------------------------------------------
# RATE COLUMN SELECTOR
# ---------------------------------------------------------------------------

def _select_rate_column(tug_count: int, movement_type: str) -> str:
    """
    Maps tug count + movement type to the correct column name.

    movement_type: "arrival" or "departure"
    tug_count    : 1 or 2 (third tug handled separately)

    Returns column key string matching the tariff matrix.
    """
    movement = movement_type.lower().strip()
    if movement not in ('arrival', 'departure'):
        raise ValueError(
            f"movement_type must be 'arrival' or 'departure', got '{movement_type}'"
        )

    if tug_count == 1:
        return f"one_tug_{movement}_usd"
    elif tug_count >= 2:
        return f"two_tugs_{movement}_usd"
    else:
        raise ValueError(f"tug_count must be 1 or 2 (use third_tug flag for 3rd tug), got {tug_count}")


# ---------------------------------------------------------------------------
# OVERTIME CALCULATION
# ---------------------------------------------------------------------------

def _calculate_overtime(
    base_rate: float,
    actual_duration_hrs: float,
    standard_duration_hrs: float = 1.0,
    increment_mins: int = 15,
    surcharge_pct_per_increment: float = 0.25
) -> float:
    """
    Calculates overtime beyond standard duration.
    Each 15-min increment (or fraction) = surcharge_pct_per_increment * base_rate.
    """
    if actual_duration_hrs <= standard_duration_hrs:
        return 0.0
    
    excess_mins = (actual_duration_hrs - standard_duration_hrs) * 60
    increments = math.ceil(excess_mins / increment_mins)
    return increments * surcharge_pct_per_increment * base_rate


# ---------------------------------------------------------------------------
# MAIN PUBLIC FUNCTION
# ---------------------------------------------------------------------------

def calculate_per_service_tug_count(
    trb_value: float,
    tug_count: int,
    movement_type: str,
    actual_duration_hrs: float,
    tariff_matrix: List[Dict],
    third_tug_requested: bool = False,
    standard_duration_hrs: float = 1.0,
    bow_thruster_discount: bool = False,
    small_vessel_discount: bool = False,
    small_vessel_threshold_trb: float = 2500.0,
    small_vessel_multiplier: float = 0.70
) -> Dict[str, Union[float, str, bool]]:
    """
    Calculates total per-service charge based on TRB bracket,
    tug count, and movement type.

    Args:
        trb_value               : Vessel TRB/GRT value
        tug_count               : Number of tugs (1 or 2 for main service)
        movement_type           : "arrival" or "departure"
        actual_duration_hrs     : Actual service duration from SOF (hours)
        tariff_matrix           : tariff_matrix_per_service_hour list from port JSON
        third_tug_requested     : If True, adds a third tug charge at the
                                  single-tug basic quota for the TRB range
        standard_duration_hrs   : Included duration before overtime kicks in
        bow_thruster_discount   : 10% discount if single tug + bow thruster
                                  (cargo/container ships only)
        small_vessel_discount   : 70% rate if vessel under threshold TRB
        small_vessel_threshold_trb : TRB cutoff for small vessel discount
        small_vessel_multiplier : Rate multiplier for small vessels

    Returns:
        dict with keys:
            total_charge         : float — total charge including overtime
            base_rate            : float — rate from matrix before discounts
            effective_rate       : float — rate after discounts applied
            overtime_charge      : float — overtime on primary service
            third_tug_charge     : float — total charge (base+ot) for 3rd tug
            discount_applied     : str
            rate_column_used     : str — column key selected from matrix
            trb_used             : float
            tug_count            : int
            movement_type        : str
            duration_hrs         : float
            null_combination     : bool — True if selected column was null
                                   (combination not permitted by tariff)
    """
    row = _match_trb_bracket(tariff_matrix, trb_value)

    if row is None:
        raise ValueError(
            f"No TRB bracket match found for TRB={trb_value}."
        )

    rate_column = _select_rate_column(tug_count, movement_type)
    base_rate = row.get(rate_column)

    # Null means this tug count / movement combination is not permitted
    if base_rate is None:
        logger.warning(
            f"Null rate encountered for Ensenada: TRB={trb_value}, "
            f"Tugs={tug_count}, Move={movement_type}, Col={rate_column}"
        )
        return {
            'total_charge': 0.0,
            'base_rate': None,
            'effective_rate': None,
            'overtime_charge': 0.0,
            'third_tug_charge': 0.0,
            'discount_applied': 'none',
            'rate_column_used': rate_column,
            'trb_used': trb_value,
            'tug_count': tug_count,
            'movement_type': movement_type,
            'duration_hrs': actual_duration_hrs,
            'null_combination': True
        }

    base_rate = float(base_rate)

    # --- Apply discounts ---
    discount_applied = "none"
    effective_rate = base_rate

    # Priority: Small vessel discount (usually takes precedence)
    if small_vessel_discount and trb_value < small_vessel_threshold_trb:
        effective_rate = base_rate * small_vessel_multiplier
        discount_applied = f"small_vessel_{int(small_vessel_multiplier * 100)}pct"

    # Bow Thruster Discount: Only for single tug operations
    elif bow_thruster_discount and tug_count == 1:
        effective_rate = base_rate * 0.90
        discount_applied = "bow_thruster_10pct"

    # --- Overtime (Primary Service) ---
    # Overtime is calculated on the effective rate (discounted rate)
    overtime_charge = _calculate_overtime(
        base_rate=effective_rate,
        actual_duration_hrs=actual_duration_hrs,
        standard_duration_hrs=standard_duration_hrs
    )

    # --- Third Tug Logic ---
    # Billed at the single-tug quota for that movement type.
    # Note: Discounts (Small Vessel/Bow Thruster) typically do NOT apply to the 3rd tug,
    # as it is an additional assist.
    third_tug_charge = 0.0
    if third_tug_requested:
        third_tug_column = f"one_tug_{movement_type.lower()}_usd"
        third_tug_base = row.get(third_tug_column)
        
        if third_tug_base is not None:
            third_tug_base = float(third_tug_base)
            # Calculate overtime specific to the 3rd tug's rate
            third_tug_ot = _calculate_overtime(
                base_rate=third_tug_base,
                actual_duration_hrs=actual_duration_hrs,
                standard_duration_hrs=standard_duration_hrs
            )
            third_tug_charge = third_tug_base + third_tug_ot
        else:
            logger.warning(f"Third tug requested but rate missing for column: {third_tug_column}")

    total_charge = effective_rate + overtime_charge + third_tug_charge

    return {
        'total_charge': round(total_charge, 2),
        'base_rate': base_rate,
        'effective_rate': round(effective_rate, 2),
        'overtime_charge': round(overtime_charge, 2),
        'third_tug_charge': round(third_tug_charge, 2),
        'discount_applied': discount_applied,
        'rate_column_used': rate_column,
        'trb_used': trb_value,
        'tug_count': tug_count,
        'movement_type': movement_type,
        'duration_hrs': actual_duration_hrs,
        'null_combination': False
    }


# ─────────────────────────────────────────────────────────────────────
# SELF TEST
# ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # Mock Tariff Matrix (Ensenada)
    tariff_matrix = [
        {
            "trb_range": "Under 2,500",
            "one_tug_arrival_usd": 500.00,
            "two_tugs_arrival_usd": 900.00,
            "one_tug_departure_usd": 450.00,
            "two_tugs_departure_usd": 850.00
        },
        {
            "trb_range": "2,501 to 5,000",
            "one_tug_arrival_usd": 700.00,
            "two_tugs_arrival_usd": None, # Not permitted for this size
            "one_tug_departure_usd": 600.00,
            "two_tugs_departure_usd": 1100.00
        }
    ]

    print("--- Test 1: Standard Arrival, 1 Tug, 1.5 Hrs ---")
    # Base 500. Overtime 30 mins (2 inc). 2 * 0.25 * 500 = 250. Total 750.
    res = calculate_per_service_tug_count(
        trb_value=2000, tug_count=1, movement_type="arrival",
        actual_duration_hrs=1.5, tariff_matrix=tariff_matrix
    )
    print(f"Total: ${res['total_charge']} (Base: ${res['base_rate']}, OT: ${res['overtime_charge']})")

    print("\n--- Test 2: Null Combination Check (Large Ship, Arrival, 2 Tugs) ---")
    # Matrix has None for two_tugs_arrival_usd in 2,501-5,000 bracket
    res = calculate_per_service_tug_count(
        trb_value=3000, tug_count=2, movement_type="arrival",
        actual_duration_hrs=1.0, tariff_matrix=tariff_matrix
    )
    print(f"Total: ${res['total_charge']} (Null Combination: {res['null_combination']})")

    print("\n--- Test 3: Small Vessel Discount ---")
    # Base 500. Discount 70% -> 350.
    res = calculate_per_service_tug_count(
        trb_value=2000, tug_count=1, movement_type="arrival",
        actual_duration_hrs=1.0, tariff_matrix=tariff_matrix,
        small_vessel_discount=True
    )
    print(f"Total: ${res['total_charge']} (Rate: ${res['effective_rate']})")

    print("\n--- Test 4: Third Tug ---")
    # Base 500 (1 tug). 3rd tug base 500. Total Base 1000.
    # 1.25 hrs = 1 inc overtime.
    # Main OT: 0.25 * 500 = 125.
    # 3rd Tug OT: 0.25 * 500 = 125.
    # Total: 1000 + 125 + 125 = 1250.
    res = calculate_per_service_tug_count(
        trb_value=2000, tug_count=1, movement_type="arrival",
        actual_duration_hrs=1.25, tariff_matrix=tariff_matrix,
        third_tug_requested=True
    )
    print(f"Total: ${res['total_charge']} (3rd Tug Charge: ${res['third_tug_charge']})")

    