"""
overtime_calculator.py
-----------------------
Calculates overtime charges for all ports based on actual vs standard duration.

Six overtime patterns found across the 25 ports:

  1. flat_hourly_per_tug
     Fixed rate per tug per hour (or 15-min increment).
     Ports: Ghent (€1,250/tug), Antwerp (€1,250/tug after 2hrs),
            Rotterdam (€1,425/tug in 15-min increments)

  2. pct_surcharge_on_assist_rate
     Percentage of the base assist rate per increment.
     Port: Le Havre (30% of assist rate per increment after time allowance)

  3. pct_of_base_per_15min
     25% of base maneuver rate per 15-min increment beyond 1 hour.
     Ports: Most Mexican ports (Altamira, Coatzacoalcos, Ensenada, Guaymas,
            Manzanillo, Mazatlan, Salina Cruz, Tampico)

  4. prorata_10min_rounding
     Actual minutes pro-rated against first-hour rate, rounded to 10 mins.
     Port: Santo Domingo / Haina

  5. first_full_hour_then_30min_increments
     First overtime period billed as full hour, then 30-min increments.
     Port: Brake (Tariff B hourly logic)

  6. fixed_hourly_amount
     Fixed amount per hour (or fraction), NOT a percentage of base rate.
     Port: Las Palmas (€1,560.89/hr standard tug, €383.38/hr small tug)
"""

import math
import logging
from dataclasses import dataclass
from typing import Optional, Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RESULT DATACLASS
# ---------------------------------------------------------------------------

@dataclass
class OvertimeResult:
    overtime_charge: float
    pattern_used: str
    excess_duration_hrs: float
    billing_units: float        # increments or hours billed
    rate_applied: float         # rate per unit used
    tug_count: int
    citation: str


# ---------------------------------------------------------------------------
# PATTERN 1 — FLAT HOURLY PER TUG
# ---------------------------------------------------------------------------

def flat_hourly_per_tug(
    actual_duration_hrs: float,
    standard_duration_hrs: float,
    hourly_rate_per_tug: float,
    tug_count: int,
    increment_mins: int = 60,
    port: str = ""
) -> OvertimeResult:
    """
    Flat rate per tug per hour (or per increment).
    Any fraction of an increment rounds up to a full increment.
    Used by: Ghent, Antwerp, Rotterdam.
    """
    if actual_duration_hrs <= standard_duration_hrs:
        return OvertimeResult(0.0, "flat_hourly_per_tug", 0.0, 0.0,
                              hourly_rate_per_tug, tug_count,
                              f"{port}: no overtime (within standard duration)")

    excess_hrs = actual_duration_hrs - standard_duration_hrs
    excess_mins = excess_hrs * 60
    increments = math.ceil(excess_mins / increment_mins)
    hrs_billed = increments * (increment_mins / 60)
    charge = hourly_rate_per_tug * hrs_billed * tug_count

    return OvertimeResult(
        overtime_charge=round(charge, 2),
        pattern_used="flat_hourly_per_tug",
        excess_duration_hrs=round(excess_hrs, 4),
        billing_units=increments,
        rate_applied=hourly_rate_per_tug,
        tug_count=tug_count,
        citation=(
            f"{port}: flat hourly overtime — "
            f"{tug_count} tug(s) × €{hourly_rate_per_tug}/hr × "
            f"{increments} increment(s) of {increment_mins} min"
        )
    )


# ---------------------------------------------------------------------------
# PATTERN 2 — PERCENTAGE SURCHARGE ON ASSIST RATE
# ---------------------------------------------------------------------------

def pct_surcharge_on_assist_rate(
    actual_duration_hrs: float,
    standard_duration_hrs: float,
    assist_rate: float,
    surcharge_pct: float,
    increment_mins: int = 60,
    port: str = ""
) -> OvertimeResult:
    """
    Overtime = surcharge_pct × assist_rate per increment beyond standard duration.
    Note: assist_rate is expected to be the total maneuver rate.
    Used by: Le Havre (30% of assist rate).
    """
    if actual_duration_hrs <= standard_duration_hrs:
        return OvertimeResult(0.0, "pct_surcharge_on_assist_rate", 0.0, 0.0,
                              assist_rate, 1, f"{port}: no overtime")

    excess_hrs = actual_duration_hrs - standard_duration_hrs
    excess_mins = excess_hrs * 60
    increments = math.ceil(excess_mins / increment_mins)
    charge = (surcharge_pct / 100) * assist_rate * increments

    return OvertimeResult(
        overtime_charge=round(charge, 2),
        pattern_used="pct_surcharge_on_assist_rate",
        excess_duration_hrs=round(excess_hrs, 4),
        billing_units=increments,
        rate_applied=assist_rate,
        tug_count=1, # Applied to total service rate, not per tug
        citation=(
            f"{port}: {surcharge_pct}% of assist rate (€{assist_rate}) × "
            f"{increments} increment(s) of {increment_mins} min"
        )
    )


# ---------------------------------------------------------------------------
# PATTERN 3 — 25% OF BASE PER 15-MIN INCREMENT
# ---------------------------------------------------------------------------

def pct_of_base_per_15min(
    actual_duration_hrs: float,
    standard_duration_hrs: float,
    base_rate: float,
    surcharge_pct: float = 25.0,
    increment_mins: int = 15,
    port: str = ""
) -> OvertimeResult:
    """
    Each 15-min increment (or fraction) beyond standard duration
    is charged at surcharge_pct% of the base maneuver rate.
    Used by: Most Mexican ports.
    """
    if actual_duration_hrs <= standard_duration_hrs:
        return OvertimeResult(0.0, "pct_of_base_per_15min", 0.0, 0.0,
                              base_rate, 1, f"{port}: no overtime")

    excess_hrs = actual_duration_hrs - standard_duration_hrs
    excess_mins = excess_hrs * 60
    increments = math.ceil(excess_mins / increment_mins)
    charge = (surcharge_pct / 100) * base_rate * increments

    return OvertimeResult(
        overtime_charge=round(charge, 2),
        pattern_used="pct_of_base_per_15min",
        excess_duration_hrs=round(excess_hrs, 4),
        billing_units=increments,
        rate_applied=base_rate,
        tug_count=1,
        citation=(
            f"{port}: {surcharge_pct}% of base rate (€{base_rate}) × "
            f"{increments} × 15-min increment(s)"
        )
    )


# ---------------------------------------------------------------------------
# PATTERN 4 — PRO-RATA 10-MIN ROUNDING ON FIRST HOUR RATE
# ---------------------------------------------------------------------------

def prorata_10min_rounding(
    actual_duration_hrs: float,
    standard_duration_hrs: float,
    first_hour_rate_per_tug: float,
    tug_count: int,
    rounding_mins: int = 10,
    port: str = ""
) -> OvertimeResult:
    """
    Overtime is pro-rated against the first-hour rate, rounded up to
    the nearest 10-minute multiple.
    Used by: Santo Domingo / Haina.
    """
    if actual_duration_hrs <= standard_duration_hrs:
        return OvertimeResult(0.0, "prorata_10min_rounding", 0.0, 0.0,
                              first_hour_rate_per_tug, tug_count,
                              f"{port}: no overtime")

    excess_hrs = actual_duration_hrs - standard_duration_hrs
    excess_mins = excess_hrs * 60
    # Round up to nearest 10-min multiple
    rounded_mins = math.ceil(excess_mins / rounding_mins) * rounding_mins
    fraction_of_hour = rounded_mins / 60
    charge = first_hour_rate_per_tug * fraction_of_hour * tug_count

    return OvertimeResult(
        overtime_charge=round(charge, 2),
        pattern_used="prorata_10min_rounding",
        excess_duration_hrs=round(excess_hrs, 4),
        billing_units=rounded_mins,
        rate_applied=first_hour_rate_per_tug,
        tug_count=tug_count,
        citation=(
            f"{port}: pro-rata overtime — {rounded_mins} min (rounded to 10) × "
            f"(€{first_hour_rate_per_tug}/hr ÷ 60) × {tug_count} tug(s)"
        )
    )


# ---------------------------------------------------------------------------
# PATTERN 5 — FIRST FULL HOUR THEN 30-MIN INCREMENTS
# ---------------------------------------------------------------------------

def first_full_hour_then_30min(
    actual_duration_hrs: float,
    standard_duration_hrs: float,
    hourly_rate_per_tug: float,
    tug_count: int,
    port: str = ""
) -> OvertimeResult:
    """
    First overtime period billed as full hour.
    Additional time billed in 30-min increments (any fraction rounds up).
    Used by: Brake (Tariff B holding/pushing/shifting).
    """
    if actual_duration_hrs <= standard_duration_hrs:
        return OvertimeResult(0.0, "first_full_hour_then_30min", 0.0, 0.0,
                              hourly_rate_per_tug, tug_count,
                              f"{port}: no overtime")

    excess_hrs = actual_duration_hrs - standard_duration_hrs

    if excess_hrs <= 1.0 + 1e-6:
        # First period: full hour charge (epsilon tolerance for floating-point)
        charge = hourly_rate_per_tug * tug_count
        billing_units = 1.0
    else:
        # First hour + 30-min increments after
        first_hour_charge = hourly_rate_per_tug * tug_count
        remaining_hrs = excess_hrs - 1.0
        remaining_mins = remaining_hrs * 60
        increments = math.ceil(remaining_mins / 30)
        additional_charge = (hourly_rate_per_tug / 2) * increments * tug_count
        charge = first_hour_charge + additional_charge
        billing_units = 1.0 + (increments * 0.5)

    return OvertimeResult(
        overtime_charge=round(charge, 2),
        pattern_used="first_full_hour_then_30min",
        excess_duration_hrs=round(excess_hrs, 4),
        billing_units=billing_units,
        rate_applied=hourly_rate_per_tug,
        tug_count=tug_count,
        citation=(
            f"{port}: first hour full (€{hourly_rate_per_tug}/tug) then "
            f"30-min increments × {tug_count} tug(s)"
        )
    )


# ---------------------------------------------------------------------------
# PATTERN 6 — FIXED HOURLY AMOUNT (not % of base)
# ---------------------------------------------------------------------------

def fixed_hourly_amount(
    actual_duration_hrs: float,
    standard_duration_hrs: float,
    hourly_rate: float,
    tug_count: int = 1,
    increment_mins: int = 60,
    port: str = ""
) -> OvertimeResult:
    """
    Fixed amount per hour or fraction — independent of base rate.
    Used by: Las Palmas (€1,560.89/hr standard tug, €383.38/hr small tug).
    """
    if actual_duration_hrs <= standard_duration_hrs:
        return OvertimeResult(0.0, "fixed_hourly_amount", 0.0, 0.0,
                              hourly_rate, tug_count, f"{port}: no overtime")

    excess_hrs = actual_duration_hrs - standard_duration_hrs
    excess_mins = excess_hrs * 60
    increments = math.ceil(excess_mins / increment_mins)
    hrs_billed = increments * (increment_mins / 60)
    charge = hourly_rate * hrs_billed * tug_count

    return OvertimeResult(
        overtime_charge=round(charge, 2),
        pattern_used="fixed_hourly_amount",
        excess_duration_hrs=round(excess_hrs, 4),
        billing_units=increments,
        rate_applied=hourly_rate,
        tug_count=tug_count,
        citation=(
            f"{port}: fixed overtime rate €{hourly_rate}/hr × "
            f"{increments} increment(s) × {tug_count} tug(s)"
        )
    )


# ---------------------------------------------------------------------------
# DISPATCHER — used by port_router
# ---------------------------------------------------------------------------

OVERTIME_PATTERNS = {
    "flat_hourly_per_tug":            flat_hourly_per_tug,
    "pct_surcharge_on_assist_rate":   pct_surcharge_on_assist_rate,
    "pct_of_base_per_15min":          pct_of_base_per_15min,
    "prorata_10min_rounding":         prorata_10min_rounding,
    "first_full_hour_then_30min":     first_full_hour_then_30min,
    "fixed_hourly_amount":            fixed_hourly_amount,
}


def calculate_overtime(
    pattern: str,
    actual_duration_hrs: float,
    standard_duration_hrs: float,
    port: str = "",
    **kwargs: Any
) -> OvertimeResult:
    """
    Entry point for the port router.

    Args:
        pattern              : One of the keys in OVERTIME_PATTERNS
        actual_duration_hrs  : Actual service duration from SOF
        standard_duration_hrs: Included duration from tariff
        port                 : Port name for citation
        **kwargs             : Pattern-specific args (rates, tug_count, etc.)

    Returns:
        OvertimeResult
    """
    fn = OVERTIME_PATTERNS.get(pattern)

    if fn is None:
        raise ValueError(
            f"Unknown overtime pattern '{pattern}' for port '{port}'. "
            f"Available: {list(OVERTIME_PATTERNS.keys())}"
        )

    return fn(
        actual_duration_hrs=actual_duration_hrs,
        standard_duration_hrs=standard_duration_hrs,
        port=port,
        **kwargs
    )


# ─────────────────────────────────────────────────────────────────────
# SELF TEST
# ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # Test Pattern 1: Flat Hourly (Ghent)
    # Excess 0.5 hrs = 30 mins. 60 min increments -> 1 increment.
    # 1250 * 1 * 2 tugs = 2500
    res = calculate_overtime(
        "flat_hourly_per_tug",
        actual_duration_hrs=1.5,
        standard_duration_hrs=1.0,
        hourly_rate_per_tug=1250,
        tug_count=2,
        port="Ghent"
    )
    print(f"[Ghent] Charge: ${res.overtime_charge} | {res.citation}")

    # Test Pattern 3: 25% per 15 min (Mexico)
    # Excess 20 mins -> 2 increments. Base 1000.
    # 0.25 * 1000 * 2 = 500
    res = calculate_overtime(
        "pct_of_base_per_15min",
        actual_duration_hrs=1.33,
        standard_duration_hrs=1.0,
        base_rate=1000,
        surcharge_pct=25.0,
        port="Coatzacoalcos"
    )
    print(f"[Mexico] Charge: ${res.overtime_charge} | {res.citation}")

    # Test Pattern 5: First Hour then 30 min (Brake)
    # Excess 1.5 hrs.
    # First hour: 1000. Remaining 0.5 hr = 1 increment (0.5 hr * rate).
    # Total = 1000 + 500 = 1500
    res = calculate_overtime(
        "first_full_hour_then_30min",
        actual_duration_hrs=2.5,
        standard_duration_hrs=1.0,
        hourly_rate_per_tug=1000,
        tug_count=1,
        port="Brake"
    )
    print(f"[Brake] Charge: ${res.overtime_charge} | {res.citation}")

    