"""
surcharge_engine.py
-------------------
Applies port-specific surcharges on top of the base rate returned
by any of the 6 calculation handlers.

Each surcharge is evaluated independently and applied to the base rate.
Multiple surcharges can stack (applied additively).

Surcharge types covered:
  - dead_ship          : vessel without propulsion/steering (1.25x - 1.5x)
  - holiday_weekend    : service outside normal hours (1.25x - 1.5x)
  - fog_visibility     : limited visibility below threshold metres (1.5x)
  - lng_vessel         : LNG-specific restrictions (2.5x)
  - deep_draft         : draft-restricted vessel (1.5x)
  - shifting           : intra/inter-area shift (1.3x - 1.4x)
  - zone               : industrial zone or bay area premium (1.25x)
  - late_order         : order placed below notice threshold (2.0x)
  - dry_dock           : dry dock / slipway maneuver (1.5x)

Design:
  - Each surcharge function returns a SurchargeResult with amount and citation
  - apply_surcharges() takes a list of applicable surcharge names + config
    and returns itemised results plus a total
  - Port router passes the surcharge config from the tariff JSON
"""

import logging
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RESULT DATACLASS
# ---------------------------------------------------------------------------

@dataclass
class SurchargeResult:
    name: str
    multiplier: float
    surcharge_amount: float   # absolute amount added (base_rate * (multiplier - 1))
    applied_to: float         # the base amount this was applied to
    citation: str             # human-readable rule reference for audit trail


@dataclass
class SurchargeReport:
    base_rate: float
    surcharges: List[SurchargeResult] = field(default_factory=list)
    total_surcharge_amount: float = 0.0
    final_rate: float = 0.0

    def __post_init__(self):
        # Ensure base rate is rounded initially to avoid float artifacts
        self.base_rate = round(float(self.base_rate), 2)
        self.final_rate = self.base_rate

    def add(self, result: SurchargeResult):
        self.surcharges.append(result)
        self.total_surcharge_amount += result.surcharge_amount
        # Round the final rate to 2 decimal places to maintain currency standards
        self.final_rate = round(self.base_rate + self.total_surcharge_amount, 2)


# ---------------------------------------------------------------------------
# INDIVIDUAL SURCHARGE EVALUATORS
# ---------------------------------------------------------------------------

def _surcharge_dead_ship(
    base_rate: float,
    multiplier: float = 1.5,
    port: str = ""
) -> SurchargeResult:
    amount = base_rate * (multiplier - 1)
    return SurchargeResult(
        name="dead_ship",
        multiplier=multiplier,
        surcharge_amount=round(amount, 2),
        applied_to=base_rate,
        citation=f"{port} dead ship surcharge: {int((multiplier-1)*100)}% on base rate".strip()
    )


def _surcharge_holiday_weekend(
    base_rate: float,
    multiplier: float = 1.25,
    port: str = "",
    window_description: str = ""
) -> SurchargeResult:
    amount = base_rate * (multiplier - 1)
    return SurchargeResult(
        name="holiday_weekend",
        multiplier=multiplier,
        surcharge_amount=round(amount, 2),
        applied_to=base_rate,
        citation=f"{port} holiday/weekend surcharge: {int((multiplier-1)*100)}% "
                 f"{window_description}".strip()
    )


def _surcharge_fog_visibility(
    base_rate: float,
    multiplier: float = 1.5,
    visibility_m: Optional[float] = None,
    threshold_m: float = 500.0,
    port: str = ""
) -> SurchargeResult:
    amount = base_rate * (multiplier - 1)
    vis_note = f"(visibility {visibility_m}m < {threshold_m}m threshold)" if visibility_m else ""
    return SurchargeResult(
        name="fog_visibility",
        multiplier=multiplier,
        surcharge_amount=round(amount, 2),
        applied_to=base_rate,
        citation=f"{port} fog/visibility surcharge: {int((multiplier-1)*100)}% {vis_note}".strip()
    )


def _surcharge_lng_vessel(
    base_rate: float,
    multiplier: float = 2.5,
    port: str = ""
) -> SurchargeResult:
    amount = base_rate * (multiplier - 1)
    return SurchargeResult(
        name="lng_vessel",
        multiplier=multiplier,
        surcharge_amount=round(amount, 2),
        applied_to=base_rate,
        citation=f"{port} LNG vessel surcharge: {int((multiplier-1)*100)}% on base rate".strip()
    )


def _surcharge_deep_draft(
    base_rate: float,
    multiplier: float = 1.5,
    port: str = ""
) -> SurchargeResult:
    amount = base_rate * (multiplier - 1)
    return SurchargeResult(
        name="deep_draft",
        multiplier=multiplier,
        surcharge_amount=round(amount, 2),
        applied_to=base_rate,
        citation=f"{port} deep draft vessel surcharge: {int((multiplier-1)*100)}% on base rate".strip()
    )


def _surcharge_shifting(
    base_rate: float,
    multiplier: float = 1.3,
    shift_type: str = "intra_area",
    port: str = ""
) -> SurchargeResult:
    amount = base_rate * (multiplier - 1)
    return SurchargeResult(
        name="shifting",
        multiplier=multiplier,
        surcharge_amount=round(amount, 2),
        applied_to=base_rate,
        citation=f"{port} shifting surcharge ({shift_type}): {int((multiplier-1)*100)}% on base rate".strip()
    )


def _surcharge_zone(
    base_rate: float,
    multiplier: float = 1.25,
    zone_name: str = "",
    port: str = ""
) -> SurchargeResult:
    amount = base_rate * (multiplier - 1)
    return SurchargeResult(
        name="zone",
        multiplier=multiplier,
        surcharge_amount=round(amount, 2),
        applied_to=base_rate,
        citation=f"{port} zone surcharge ({zone_name}): {int((multiplier-1)*100)}% on base rate".strip()
    )


def _surcharge_late_order(
    base_rate: float,
    multiplier: float = 2.0,
    notice_hrs: Optional[float] = None,
    threshold_hrs: float = 4.0,
    port: str = ""
) -> SurchargeResult:
    amount = base_rate * (multiplier - 1)
    notice_note = f"(order placed {notice_hrs}hrs before service, threshold {threshold_hrs}hrs)" \
                  if notice_hrs is not None else ""
    return SurchargeResult(
        name="late_order",
        multiplier=multiplier,
        surcharge_amount=round(amount, 2),
        applied_to=base_rate,
        citation=f"{port} late order surcharge: {int((multiplier-1)*100)}% {notice_note}".strip()
    )


def _surcharge_dry_dock(
    base_rate: float,
    multiplier: float = 1.5,
    port: str = ""
) -> SurchargeResult:
    amount = base_rate * (multiplier - 1)
    return SurchargeResult(
        name="dry_dock",
        multiplier=multiplier,
        surcharge_amount=round(amount, 2),
        applied_to=base_rate,
        citation=f"{port} dry dock / slipway surcharge: {int((multiplier-1)*100)}% on base rate".strip()
    )


# ---------------------------------------------------------------------------
# DISPATCHER MAP
# ---------------------------------------------------------------------------

_SURCHARGE_MAP = {
    "dead_ship":       _surcharge_dead_ship,
    "holiday_weekend": _surcharge_holiday_weekend,
    "fog_visibility":  _surcharge_fog_visibility,
    "lng_vessel":      _surcharge_lng_vessel,
    "deep_draft":      _surcharge_deep_draft,
    "shifting":        _surcharge_shifting,
    "zone":            _surcharge_zone,
    "late_order":      _surcharge_late_order,
    "dry_dock":        _surcharge_dry_dock,
}


# ---------------------------------------------------------------------------
# MAIN PUBLIC FUNCTION
# ---------------------------------------------------------------------------

def apply_surcharges(
    base_rate: float,
    applicable_surcharges: List[Dict[str, Any]],
    port: str = ""
) -> SurchargeReport:
    """
    Applies a list of surcharges to a base rate and returns an itemised report.

    Args:
        base_rate             : The rate returned by the calculation handler
        applicable_surcharges : List of dicts, each describing one surcharge.
                                Each dict must have a 'type' key matching a
                                known surcharge name, plus any optional params.
        port                  : Port name for citation strings

    Returns:
        SurchargeReport with itemized surcharges and final total.
    """
    if not isinstance(applicable_surcharges, list):
        logger.error(f"applicable_surcharges must be a list, got {type(applicable_surcharges)}")
        return SurchargeReport(base_rate=base_rate)

    report = SurchargeReport(base_rate=base_rate)

    for surcharge_def in applicable_surcharges:
        # Ensure we are dealing with a dict
        if not isinstance(surcharge_def, dict):
            logger.warning(f"Skipping invalid surcharge definition (not a dict): {surcharge_def}")
            continue

        surcharge_type = surcharge_def.get('type', '').lower().strip()

        if surcharge_type not in _SURCHARGE_MAP:
            logger.warning(
                f"Unknown surcharge type '{surcharge_type}' for port '{port}' — skipped."
            )
            continue

        fn = _SURCHARGE_MAP[surcharge_type]

        # Build kwargs from the surcharge_def, excluding 'type'
        # Surcharges stack ADDITIVELY on the original base_rate (not multiplicatively).
        # E.g. two 1.25× surcharges on base=1000: each adds 250 → total = 1500, not 1000×1.25×1.25=1562.50.
        # This matches port tariff billing practice where each surcharge is independent.
        kwargs = {k: v for k, v in surcharge_def.items() if k != 'type'}
        kwargs['base_rate'] = report.base_rate
        kwargs['port'] = port

        try:
            result = fn(**kwargs)
            report.add(result)
        except TypeError as e:
            logger.error(
                f"Surcharge '{surcharge_type}' for port '{port}' failed with params "
                f"{kwargs}: {e}"
            )
        except Exception as e:
            logger.error(
                f"Unexpected error applying surcharge '{surcharge_type}': {e}"
            )

    return report


# ─────────────────────────────────────────────────────────────────────
# SELF TEST
# ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    base = 1000.00
    
    surcharges = [
        {"type": "dead_ship", "multiplier": 1.5}, # Adds 500
        {"type": "holiday_weekend", "multiplier": 1.25, "window_description": "Sunday 0800-1200"}, # Adds 250
        {"type": "fog_visibility", "visibility_m": 200, "threshold_m": 500}, # Adds 500 (1.5 default)
        {"type": "unknown_surcharge"}, # Should be skipped with warning
    ]

    print(f"Base Rate: ${base}")
    report = apply_surcharges(base, surcharges, port="TestPort")

    print(f"\n--- Report ---")
    print(f"Final Rate: ${report.final_rate}")
    print(f"Total Surcharges: ${report.total_surcharge_amount}")
    
    for s in report.surcharges:
        print(f" - {s.name}: +${s.surcharge_amount} ({s.citation})")
        
    # Calculation check: 
    # Base 1000
    # DS: 1000 * (1.5 - 1) = 500
    # HW: 1000 * (1.25 - 1) = 250
    # Fog: 1000 * (1.5 - 1) = 500
    # Total: 1000 + 1250 = 2250
    assert report.final_rate == 2250.00

    