"""
hourly_with_minimum.py
----------------------
Handles rate calculation for ports that bill purely by time (hourly),
with no vessel dimension (LOA/GT/GRT) driving the base rate.

Port covered: Panama

Billing patterns found across Panama areas:
  Pattern A — Simple hourly: base_rate × hours, prorated per 15 min
              e.g. Balboa Port Terminals: $2,163/hr
  Pattern B — Block + overflow: flat rate for first N hours,
              then different rate per additional hour, prorated per 15 min
              e.g. Panama Bay: $6,128.50 for first 3 hrs,
                   then $1,957/hr after
  Pattern C — Hourly with minimum floor: hourly rate but total
              cannot go below minimum_charge
              e.g. Chiriqui Grande (SPM): $1,545/hr, min $41,200

All patterns prorate every 15 minutes beyond the base unit.
"""

import math
import re
import logging
from typing import Optional, Dict, Union

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PRORATION UTILITY
# ---------------------------------------------------------------------------

def _prorate_15min(hourly_rate: float, hours: float) -> float:
    """
    Calculates charge for a given duration at an hourly rate,
    prorated in 15-minute increments (any fraction rounds up).

    Args:
        hourly_rate : rate per full hour
        hours       : duration in hours (can be fractional)

    Returns:
        float: charge for that duration
    """
    if hours <= 0:
        return 0.0
    total_mins = hours * 60
    # Ceiling division ensures any part of 15 mins is charged as a full 15 mins
    increments = math.ceil(total_mins / 15)
    return (increments / 4) * hourly_rate  # 4 increments per hour


# ---------------------------------------------------------------------------
# MAIN PUBLIC FUNCTION
# ---------------------------------------------------------------------------

def calculate_hourly_with_minimum(
    actual_duration_hrs: float,
    area_tariff: Dict[str, any],
    tug_count: int = 1,
    rate_is_per_tug: bool = True
) -> Dict[str, Union[float, str, bool]]:
    """
    Calculates total hourly charge for a given area and duration.

    Args:
        actual_duration_hrs : Actual service duration in hours (from SOF).
                              Counts from tug leaving station to returning.
        area_tariff         : Dict for the specific area from the port JSON.
                              Expected keys (depending on pattern):
                                Pattern A: base_rate_usd, billing_unit="per hour"
                                Pattern B: base_rate_usd, billing_unit="first N hours",
                                           additional_time_logic (parsed for overflow rate)
                                Pattern C: base_rate_usd, minimum_charge_usd
        tug_count           : Number of tugs.
                              Multiplied into the total if rate_is_per_tug is True.
        rate_is_per_tug     : If True, multiplies the final charge by tug_count.
                              If False (e.g., Panama Bay), the rate is a flat service fee.
                              Defaults to True.

    Returns:
        dict with keys:
            total_charge        : float — total charge
            pattern_used        : str   — "simple_hourly" | "block_plus_overflow" | "hourly_minimum"
            base_charge         : float — charge for base period
            overflow_charge     : float — charge beyond base period (0 if Pattern A)
            minimum_applied     : bool  — True if minimum floor was enforced
            minimum_charge      : float — minimum floor value (0 if not applicable)
            duration_hrs        : float
            tug_count           : int
            area                : str
    """
    area_name = area_tariff.get('area', 'unknown')
    base_rate = float(area_tariff.get('base_rate_usd', 0))
    billing_unit = str(area_tariff.get('billing_unit', 'per hour')).lower()
    minimum_charge = float(area_tariff.get('minimum_charge_usd', 0))
    additional_time_logic = str(area_tariff.get('additional_time_logic', ''))

    # --- Detect Pattern B: Block + Overflow ---
    # Looks for "first 3 hours" or "first 3 hrs"
    block_match = re.search(r'first\s+(\d+(?:\.\d+)?)\s+hrs?', billing_unit)
    
    if block_match:
        block_hours = float(block_match.group(1))
        
        # Extract additional hourly rate from additional_time_logic string
        # Handles: "$1,957 per additional hour", "1957 per additional hr", "$1957/add hr"
        add_rate_match = re.search(r'[\$]?([\d,]+(?:\.\d+)?)\s*(?:per|/)\s*additional', additional_time_logic)
        
        if not add_rate_match:
            logger.warning(f"Could not parse additional rate for area {area_name}. Logic: '{additional_time_logic}'")
            additional_rate = 0.0
        else:
            additional_rate = float(add_rate_match.group(1).replace(',', ''))

        if actual_duration_hrs <= block_hours:
            base_charge = base_rate
            overflow_charge = 0.0
        else:
            base_charge = base_rate
            overflow_hrs = actual_duration_hrs - block_hours
            overflow_charge = _prorate_15min(additional_rate, overflow_hrs)

        total = base_charge + overflow_charge
        
        # Apply Tug Count Multiplier only if rate is per tug (e.g. Cristobal)
        # If per service (e.g. Panama Bay), leave as is.
        if rate_is_per_tug:
            total *= tug_count
            base_charge_display = base_charge * tug_count
            overflow_charge_display = overflow_charge * tug_count
        else:
            base_charge_display = base_charge
            overflow_charge_display = overflow_charge

        # Check Minimum (Rare for Block+Overflow, but possible)
        minimum_applied = False
        if minimum_charge > 0 and total < minimum_charge:
            total = minimum_charge
            minimum_applied = True

        return {
            'total_rate': round(total, 2),
            'total_charge': round(total, 2),
            'pattern_used': 'block_plus_overflow',
            'base_charge': round(base_charge_display, 2),
            'overflow_charge': round(overflow_charge_display, 2),
            'minimum_applied': minimum_applied,
            'minimum_charge': minimum_charge,
            'duration_hrs': actual_duration_hrs,
            'tug_count': tug_count,
            'area': area_name
        }

    # --- Detect Pattern C: Hourly with Minimum ---
    elif minimum_charge > 0:
        # Calculate raw hourly charge
        charge_before_min = _prorate_15min(base_rate, actual_duration_hrs)
        
        if rate_is_per_tug:
            charge_before_min *= tug_count
            
        minimum_applied = charge_before_min < minimum_charge
        total = max(charge_before_min, minimum_charge)

        return {
            'total_rate': round(total, 2),
            'total_charge': round(total, 2),
            'pattern_used': 'hourly_minimum',
            'base_charge': round(charge_before_min, 2),
            'overflow_charge': 0.0,
            'minimum_applied': minimum_applied,
            'minimum_charge': minimum_charge,
            'duration_hrs': actual_duration_hrs,
            'tug_count': tug_count,
            'area': area_name
        }

    # --- Pattern A: Simple Hourly ---
    else:
        charge = _prorate_15min(base_rate, actual_duration_hrs)
        
        if rate_is_per_tug:
            charge *= tug_count

        return {
            'total_rate': round(charge, 2),
            'total_charge': round(charge, 2),
            'pattern_used': 'simple_hourly',
            'base_charge': round(charge, 2),
            'overflow_charge': 0.0,
            'minimum_applied': False,
            'minimum_charge': 0.0,
            'duration_hrs': actual_duration_hrs,
            'tug_count': tug_count,
            'area': area_name
        }


# ─────────────────────────────────────────────────────────────────────
# SELF TEST
# ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # 1. Pattern A: Simple Hourly (Balboa)
    print("--- Test 1: Simple Hourly (Per Tug) ---")
    tariff_a = {
        "area": "Balboa Terminals",
        "base_rate_usd": 2163.00,
        "billing_unit": "per hour"
    }
    # 1.25 hours = 5 increments of 15 min. 
    # (5/4) * 2163 = 2703.75 per tug.
    res = calculate_hourly_with_minimum(1.25, tariff_a, tug_count=1)
    print(f"Total: ${res['total_charge']} (Expected ~$2,703.75)")

    # 2. Pattern B: Block + Overflow (Panama Bay - Per Service)
    print("\n--- Test 2: Block + Overflow (Per Service) ---")
    tariff_b = {
        "area": "Panama Bay",
        "base_rate_usd": 6128.50,
        "billing_unit": "first 3 hours",
        "additional_time_logic": "$1,957 per additional hour, prorated every 15 minutes"
    }
    # 3.5 hours. Base 6128.50. Excess 0.5 hrs = 2 increments.
    # Overflow = (2/4) * 1957 = 978.50. Total = 7107.00.
    # Pass rate_is_per_tug=False because Panama Bay is a flat service fee.
    res = calculate_hourly_with_minimum(3.5, tariff_b, tug_count=2, rate_is_per_tug=False)
    print(f"Total: ${res['total_charge']} (Expected $7,107.00)")
    print(f"Details: Base ${res['base_charge']} + Overflow ${res['overflow_charge']}")

    # 3. Pattern B: Per Tug variation (e.g. Cristobal)
    print("\n--- Test 3: Block + Overflow (Per Tug) ---")
    res = calculate_hourly_with_minimum(3.5, tariff_b, tug_count=2, rate_is_per_tug=True)
    print(f"Total (2 tugs): ${res['total_charge']} (Expected $14,214.00)")

    # 4. Pattern C: Hourly with Minimum
    print("\n--- Test 4: Hourly with Minimum ---")
    tariff_c = {
        "area": "Chiriqui Grande",
        "base_rate_usd": 1545.00,
        "billing_unit": "per hour",
        "minimum_charge_usd": 41200.00
    }
    # 5 hours @ 1545 = 7725. Min is 41200.
    res = calculate_hourly_with_minimum(5.0, tariff_c, tug_count=1)
    print(f"Total: ${res['total_charge']} (Expected $41,200.00 - Minimum Applied)")
    print(f"Minimum Applied: {res['minimum_applied']}")

    