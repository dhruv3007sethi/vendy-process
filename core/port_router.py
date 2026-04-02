
"""
port_router.py
--------------
Entry point for the calculation pipeline.

Given a port name, SOF data, invoice line items, and the tariff JSON,
the router:
  1. Loads the port's calculation profile
  2. Resolves inputs from SOF + invoice
  3. Calls the correct handler
  4. Applies surcharges
  5. Calculates overtime
  6. Formats and returns the result
"""

import logging
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

# Assuming these calculation modules are in a 'handlers' subdirectory or accessible in path
# Adjust these paths based on your actual project structure (e.g., core.handlers...)
from handlers.bracket_lookup import (
    lookup_bracket_rate,
    calculate_oversize_supplement,
    calculate_large_vessel_increment
)
from handlers.fixed_plus_variable import calculate_fixed_plus_variable
from handlers.hp_hourly import calculate_hp_hourly
from handlers.hourly_with_minimum import calculate_hourly_with_minimum
from handlers.per_service_tug_count import calculate_per_service_tug_count
from handlers.bracket_lookup_with_formula import calculate_bracket_with_formula
from surcharge_engine import apply_surcharges
from overtime_calculator import calculate_overtime
from output_formatter import build_line_item, build_result, to_dict
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# OVERTIME PATTERN MAPPING
# Profile overtime_handler strings → overtime_calculator pattern keys
# ---------------------------------------------------------------------------

OVERTIME_PATTERN_MAP = {
    "hourly_supplement_at_1250_per_tug":             "flat_hourly_per_tug",
    "hourly_supplement_at_1250_per_tug_after_2hrs":  "flat_hourly_per_tug",
    "hourly_at_1425_per_tug_in_15min_increments":    "flat_hourly_per_tug",
    "30pct_surcharge_on_assist_rate_after_time_allowance": "pct_surcharge_on_assist_rate",
    "25pct_of_base_per_15min_after_1hr":             "pct_of_base_per_15min",
    "prorata_10min_rounding_on_first_hour_rate":     "prorata_10min_rounding",
    "first_60min_full_hour_then_30min_increments":   "first_full_hour_then_30min",
    "prorated_every_15min":                          "flat_hourly_per_tug",  # Panama
    "fixed_hourly_amount_per_tug":                   "fixed_hourly_amount",  # Las Palmas
}

# Flat overtime rates for ports where it is not a percentage of base
OVERTIME_RATES = {
    "Ghent":              {"hourly_rate_per_tug": 1250.0, "increment_mins": 60},
    "Antwerp":            {"hourly_rate_per_tug": 1250.0, "increment_mins": 60},
    "Rotterdam":          {"hourly_rate_per_tug": 1425.0, "increment_mins": 15},
}


# ---------------------------------------------------------------------------
# EUROPEAN NUMBER FORMAT NORMALISER
# ---------------------------------------------------------------------------

def _parse_vessel_dimension(raw) -> float:
    """
    Safely parse a vessel dimension (GT, GRT, LOA, TRB) from invoice data,
    handling European number formatting where '.' is a thousands separator.

    European invoices write 23,403 GT as "23.403" — not 23.4.
    Rules applied to string values:
      - Multiple dots (e.g. "1.234.567") → all are thousands separators → strip dots
      - Single dot with exactly 3 trailing digits (e.g. "23.403") → thousands separator → strip dot
      - Single dot with 1–2 trailing digits (e.g. "23.40", "23.4") → genuine decimal
      - Comma present (e.g. "23,403" or "23.403,00") → comma is decimal, dots are thousands

    Already-numeric values are returned as-is (no string parsing needed).
    """
    if raw is None:
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw)

    s = str(raw).strip().replace(" ", "")
    if not s:
        return 0.0

    # If both '.' and ',' are present: European format (. = thousands, , = decimal)
    if "." in s and "," in s:
        s = s.replace(".", "").replace(",", ".")
        return float(s)

    # Only commas, no dots: check whether comma is decimal or thousands separator.
    # Rule: exactly 1–2 digits after comma → decimal (e.g. "199,99" → 199.99)
    #       exactly 3 digits after comma    → thousands separator (e.g. "23,403" → 23403)
    if "," in s and "." not in s:
        comma_parts = s.split(",")
        if len(comma_parts) == 2 and len(comma_parts[1]) in (1, 2):
            # Decimal comma (e.g. "199,99" or "199,9")
            s = s.replace(",", ".")
            return float(s)
        else:
            # Thousands comma (e.g. "23,403") — strip all commas
            s = s.replace(",", "")
            return float(s)

    # Only dots:
    dot_parts = s.split(".")
    if len(dot_parts) > 2:
        # Multiple dots → all thousands separators (e.g. "1.234.567")
        s = s.replace(".", "")
        return float(s)
    if len(dot_parts) == 2:
        integer_part, frac_part = dot_parts
        if len(frac_part) == 3:
            # Exactly 3 digits after dot → European thousands separator
            s = integer_part + frac_part
            return float(s)
        # 1–2 digits after dot → genuine decimal
        return float(s)

    return float(s)


# ---------------------------------------------------------------------------
# MULTI-VESSEL SOF RESOLVER
# ---------------------------------------------------------------------------

def _resolve_sof_for_vessel(sof_data: dict, target_vessel: str) -> dict:
    """
    If the SOF contains multiple vessels, returns the sub-dict whose events
    correspond to the vessel matching target_vessel (invoice vessel name).

    Handles three SOF shapes:
      1. Single vessel — sof_data["vessel"] matches → return sof_data as-is.
      2. vessels array — list of per-vessel dicts each with "vessel" + "events".
      3. vessel_events dict — keyed by vessel name, values are event lists or dicts.

    Falls back to sof_data unchanged if no better match is found.
    """
    if not target_vessel:
        return sof_data

    target_lower = target_vessel.strip().lower()

    def _matches(name_raw) -> bool:
        if not name_raw:
            return False
        name = str(name_raw).strip().lower()
        return target_lower in name or name in target_lower

    # 1. Check primary vessel first
    primary_raw = sof_data.get("vessel") or sof_data.get("vessel_info") or {}
    primary_name = (
        primary_raw.get("name", "") if isinstance(primary_raw, dict) else str(primary_raw)
    )
    if _matches(primary_name):
        return sof_data

    # 2. Search vessels array
    for entry in sof_data.get("vessels", []):
        if not isinstance(entry, dict):
            continue
        v = entry.get("vessel") or entry.get("vessel_info") or {}
        v_name = v.get("name", "") if isinstance(v, dict) else str(v)
        if _matches(v_name) or _matches(entry.get("vessel_name", "")):
            logger.info(f"Multi-vessel SOF: matched '{target_vessel}' in vessels array.")
            return entry

    # 3. Search vessel_events dict keyed by name
    for key, val in sof_data.get("vessel_events", {}).items():
        if _matches(key):
            logger.info(f"Multi-vessel SOF: matched '{target_vessel}' in vessel_events.")
            if isinstance(val, dict):
                return val
            if isinstance(val, list):
                return {"events": val}

    # No match — warn and fall back to primary SOF
    logger.warning(
        f"SOF vessel '{primary_name}' does not match invoice vessel '{target_vessel}'. "
        "Using primary SOF events as fallback."
    )
    return sof_data


# ---------------------------------------------------------------------------
# SOF EVENT RESOLVER
# ---------------------------------------------------------------------------

def _resolve_sof_event(sof_data: dict, service_type: str, service_date: str = "") -> Optional[dict]:
    """
    Finds the SOF event matching the service type and (optionally) date.
    Returns the matching event dict or None.
    Only considers SOF events on the invoice line's service_date when provided,
    ignoring events outside the invoice period.
    """
    events = sof_data.get("events") or sof_data.get("service_events") or []
    service_type_lower = service_type.lower()

    def _date_ok(event: dict) -> bool:
        if not service_date:
            return True
        event_date = str(event.get("date") or event.get("timestamp") or "")[:10]
        if not event_date:
            return True
        if event_date == service_date:
            return True
        # ±1 day tolerance: handles late-night maneuvers logged on the next calendar day
        # (e.g. service at 23:55 Jan 10 written in SOF as Jan 11 by port pilot)
        try:
            svc_dt = datetime.strptime(service_date, "%Y-%m-%d")
            evt_dt = datetime.strptime(event_date, "%Y-%m-%d")
            return abs((evt_dt - svc_dt).days) <= 1
        except (ValueError, TypeError):
            return False

    # Pass 1: exact match (prevents "berth" matching "unberth")
    for event in events:
        if not _date_ok(event):
            continue
        event_type = str(event.get("type") or event.get("service_type") or "").lower()
        if event_type == service_type_lower:
            return event

    # Pass 2: fuzzy match (service description contained in event type or vice versa)
    for event in events:
        if not _date_ok(event):
            continue
        event_type = str(event.get("type") or event.get("service_type") or "").lower()
        if service_type_lower in event_type or event_type in service_type_lower:
            return event

    return None


def _get_duration_hrs(sof_event: Optional[dict], invoice_line: dict) -> float:
    """
    Returns actual service duration in hours.
    Prefers SOF event duration; falls back to invoice if not in SOF.
    """
    if sof_event:
        duration = sof_event.get("duration_hrs") or sof_event.get("duration_hours")
        if duration is not None:
            return float(duration)

    # Fallback to invoice
    return float(invoice_line.get("duration_hrs") or invoice_line.get("duration_hours") or 1.0)


def _get_tug_count(sof_event: Optional[dict], invoice_line: dict, tug_count_source: str) -> int:
    """
    Returns tug count.
    If tug_count_source is "invoice", always uses invoice value.
    Otherwise cross-checks SOF.
    """
    invoice_tugs = int(invoice_line.get("tug_count") or invoice_line.get("tugs") or 1)

    if tug_count_source == "invoice":
        return invoice_tugs

    if sof_event:
        sof_tugs = sof_event.get("tug_count") or sof_event.get("tugs")
        if sof_tugs is not None:
            return int(sof_tugs)

    return invoice_tugs


# ---------------------------------------------------------------------------
# SURCHARGE BUILDER
# ---------------------------------------------------------------------------

def _build_surcharge_list(invoice_line: dict, profile: dict, port: str) -> List[Dict[str, Any]]:
    """
    Builds the list of applicable surcharges from invoice line flags.
    """
    surcharges = []
    multipliers = profile.get("surcharge_multipliers") or {}

    if invoice_line.get("dead_ship"):
        mult = multipliers.get("dead_ship", 1.5)
        surcharges.append({"type": "dead_ship", "multiplier": mult})

    if invoice_line.get("holiday") or invoice_line.get("weekend"):
        mult = multipliers.get("sunday_public_holiday", 1.25)
        surcharges.append({"type": "holiday_weekend", "multiplier": mult})

    if invoice_line.get("fog") or invoice_line.get("limited_visibility"):
        surcharges.append({"type": "fog_visibility", "multiplier": 1.5})

    if invoice_line.get("lng_vessel"):
        surcharges.append({"type": "lng_vessel", "multiplier": 2.5})

    if invoice_line.get("deep_draft"):
        surcharges.append({"type": "deep_draft", "multiplier": 1.5})

    if invoice_line.get("shifting"):
        mult = multipliers.get("shifting", 1.3)
        surcharges.append({
            "type": "shifting", 
            "multiplier": mult,
            "shift_type": invoice_line.get("shift_type", "intra_area")
        })

    if invoice_line.get("dry_dock"):
        surcharges.append({"type": "dry_dock", "multiplier": 1.5})

    if invoice_line.get("late_order"):
        surcharges.append({"type": "late_order", "multiplier": 2.0})

    return surcharges


# ---------------------------------------------------------------------------
# RATIO-BASED BACKWARD INFERENCE ENGINE
# ---------------------------------------------------------------------------

# Surcharge defaults used when not overridden in port profile
_SURCHARGE_ENGINE_DEFAULTS = {
    "holiday_weekend":  1.25,
    "fog_visibility":   1.50,
    "deep_draft":       1.50,
    "late_order":       2.00,
    "lng_vessel":       2.50,
    "dry_dock":         1.50,
    "dead_ship":        1.50,
    "shifting":         1.30,
}


def _infer_plausible_conditions(
    profile: dict,
    dim_value: float,
    invoiced_amount: float,
    known_surcharge_types: List[str],
    plausible_tolerance_pct: float = 3.0
) -> List[Dict[str, Any]]:
    """
    Ratio-based backward inference: given invoiced amount and the tariff profile,
    infers which combination of zone / surcharge / waiting-time / delay-discount
    could explain the invoiced amount within plausible_tolerance_pct.

    Returns a list of candidate explanation dicts:
        {type, description, expected_amount, variance_pct}

    Noise suppression: if any candidate has variance_pct == 0.0, all non-zero
    candidates are suppressed (they are mathematical coincidences, not explanations).
    """
    if invoiced_amount <= 0:
        return []

    candidates: List[Dict[str, Any]] = []
    rates = profile.get("rates", {})
    profile_surcharges = profile.get("surcharge_multipliers", {})
    waiting = profile.get("waiting_time", {})
    delay_bonuses = profile.get("provider_delay_bonuses", {})

    # Build full surcharge map: profile overrides defaults; skip known/flagged ones
    all_surcharges = {**_SURCHARGE_ENGINE_DEFAULTS, **profile_surcharges}
    unknown_surcharges = {
        k: v for k, v in all_surcharges.items()
        if k not in known_surcharge_types
    }

    def _variance(expected: float) -> float:
        return round(abs(invoiced_amount - expected) / invoiced_amount * 100, 2)

    def _within(variance: float) -> bool:
        return variance <= plausible_tolerance_pct

    # --- Level 1: Zone variants ---
    for zone, zone_rates in rates.items():
        if not isinstance(zone_rates, dict):
            continue
        try:
            result = calculate_fixed_plus_variable(dim_value, zone_rates, zone)
            expected = round(float(result.get("base_rate") or 0.0), 2)
            v = _variance(expected)
            if _within(v):
                candidates.append({
                    "type": "zone",
                    "description": (
                        f"Could be correct if zone = '{zone}' applies "
                        f"(expected EUR {expected:,.2f}, {v:.1f}% variance)"
                    ),
                    "expected_amount": expected,
                    "variance_pct": v,
                })
        except Exception:
            pass

    # Compute base using the first (default/fallback) zone for Levels 2–5
    default_zone, default_rates = next(iter(rates.items()), (None, None))
    base_rate = 0.0
    if default_rates and isinstance(default_rates, dict):
        try:
            result = calculate_fixed_plus_variable(dim_value, default_rates, default_zone)
            base_rate = round(float(result.get("base_rate") or 0.0), 2)
        except Exception:
            pass

    if base_rate > 0:
        ratio = invoiced_amount / base_rate

        # --- Level 2: Single surcharge on default zone ---
        for surcharge_name, multiplier in unknown_surcharges.items():
            expected = round(base_rate * multiplier, 2)
            v = _variance(expected)
            if _within(v):
                candidates.append({
                    "type": "surcharge",
                    "description": (
                        f"'{surcharge_name}' surcharge (×{multiplier}) on base rate "
                        f"may have been applied (expected EUR {expected:,.2f}, {v:.1f}% variance)"
                    ),
                    "expected_amount": expected,
                    "variance_pct": v,
                })

        # --- Level 3: Zone + single surcharge combinations ---
        for zone, zone_rates in rates.items():
            if not isinstance(zone_rates, dict):
                continue
            try:
                zone_result = calculate_fixed_plus_variable(dim_value, zone_rates, zone)
                zone_base = round(float(zone_result.get("base_rate") or 0.0), 2)
            except Exception:
                continue
            for surcharge_name, multiplier in unknown_surcharges.items():
                expected = round(zone_base * multiplier, 2)
                v = _variance(expected)
                if _within(v):
                    candidates.append({
                        "type": "zone+surcharge",
                        "description": (
                            f"Could be correct if zone = '{zone}' AND "
                            f"'{surcharge_name}' surcharge (×{multiplier}) applies "
                            f"(expected EUR {expected:,.2f}, {v:.1f}% variance)"
                        ),
                        "expected_amount": expected,
                        "variance_pct": v,
                    })

        # --- Level 4: Waiting time addition ---
        waiting_rate = float(waiting.get("hourly_standby_rate_eur") or 0.0)
        if waiting_rate > 0:
            diff = invoiced_amount - base_rate
            if diff > 0:
                raw_hrs = diff / waiting_rate
                # Round to nearest 0.5-hr increment
                rounded_hrs = round(raw_hrs * 2) / 2
                if rounded_hrs > 0:
                    expected = round(base_rate + rounded_hrs * waiting_rate, 2)
                    v = _variance(expected)
                    if _within(v):
                        candidates.append({
                            "type": "waiting_time",
                            "description": (
                                f"Waiting time of ~{rounded_hrs:.1f} hrs at "
                                f"EUR {waiting_rate:,.2f}/hr may explain the additional "
                                f"EUR {diff:,.2f} (expected EUR {expected:,.2f}, {v:.1f}% variance)"
                            ),
                            "expected_amount": expected,
                            "variance_pct": v,
                        })

        # --- Level 5: Provider delay discount ---
        for tier in delay_bonuses.get("tiers", []):
            discount_mult = float(tier.get("multiplier") or 1.0)
            discount_pct = tier.get("discount_pct", 0)
            if discount_mult >= 1.0:
                continue  # Only handle discounts (< 1.0)
            expected = round(base_rate * discount_mult, 2)
            v = _variance(expected)
            if _within(v):
                candidates.append({
                    "type": "delay_discount",
                    "description": (
                        f"Provider delay discount ({discount_pct}%) may have been applied "
                        f"(expected EUR {expected:,.2f}, {v:.1f}% variance)"
                    ),
                    "expected_amount": expected,
                    "variance_pct": v,
                })

    # --- Noise suppression ---
    # If any candidate is an exact match (0.0% variance), suppress all near-matches
    if any(c["variance_pct"] == 0.0 for c in candidates):
        candidates = [c for c in candidates if c["variance_pct"] == 0.0]

    return candidates


# ---------------------------------------------------------------------------
# PER-PORT HANDLER DISPATCH
# ---------------------------------------------------------------------------

def _dispatch_handler(
    port: str,
    profile: dict,
    tariff_data: dict,
    invoice_line: dict,
    sof_event: Optional[dict],
    tug_count: int
) -> dict:
    """
    Calls the correct calculation handler for the port.
    Returns the handler result dict.
    """
    pattern = profile.get("calculation_pattern")
    dim_type = profile.get("dimension_type", "")
    
    # Resolve dimension value (GT, LOA, GRT, etc.) from invoice line.
    # Priority order is determined by the port's dimension_type so that a line
    # carrying both 'loa' and 'gt' uses the correct one for each port.
    if dim_type in ("GT",):
        _raw_dim = invoice_line.get("gt") or invoice_line.get("loa")
    elif dim_type in ("GRT",):
        _raw_dim = invoice_line.get("grt") or invoice_line.get("trb") or invoice_line.get("gt")
    else:  # LOA_meters, LOA, or unspecified
        _raw_dim = invoice_line.get("loa") or invoice_line.get("gt") or invoice_line.get("grt")
    dim_value = _parse_vessel_dimension(_raw_dim)

    # --- bracket_lookup (Ghent, Antwerp, etc.) ---
    if pattern == "bracket_lookup":
        zone = invoice_line.get("zone") or invoice_line.get("route")
        route_hint = invoice_line.get("route_hint")
        rates_data = _get_rates_data(tariff_data, zone, route_hint)
        # Zone-specific rate column (e.g. Antwerp: zandvliet_kallo_rate_eur vs antwerp_city_rate_eur)
        rate_col = profile.get("zone_rate_columns", {}).get(zone) if zone else None
        base_rate = lookup_bracket_rate(rates_data, dim_value, rate_col)

        # MXN → USD conversion (Guaymas and similar Mexican bracket ports)
        fx_rate = float(invoice_line.get("fx_rate_mxn_usd") or 0)
        if fx_rate > 0:
            base_rate = round(base_rate / fx_rate, 2)

        # Large vessel increment (Ghent)
        lvi_charge = 0.0
        lvi = tariff_data.get("logic_rules", {}).get("large_vessel_increment")
        if lvi and dim_type == "LOA_meters":
            lvi_charge = calculate_large_vessel_increment(
                loa=dim_value,
                apply_above_meters=lvi["apply_above_meters"],
                increment_step_meters=lvi["increment_step_meters"],
                surcharge_per_step=lvi["surcharge_per_step"]
            )

        # Oversize (Rostock, Brake)
        oversize_charge = 0.0
        oversize = tariff_data.get("logic_rules", {}).get("oversize_logic") or \
                   tariff_data.get("oversize_logic")
        if oversize:
            oversize_charge = calculate_oversize_supplement(
                dimension_value=dim_value,
                threshold=oversize.get("gt_threshold", 0),
                increment_unit=oversize.get("increment_unit_gt", 10000),
                rate_per_increment=oversize.get("increment_rate_eur", 0)
            )

        # Multiply by tug_count only for per_tug_per_move billing (Ghent, Antwerp, etc.)
        # per_service ports (Guaymas, Santo Domingo, most Mexican) bill once per maneuver.
        billing_basis = profile.get("billing_basis", "per_tug_per_move")
        tug_multiplier = tug_count if billing_basis == "per_tug_per_move" else 1
        return {
            "base_rate": base_rate,
            "total_rate": round((base_rate + lvi_charge + oversize_charge) * tug_multiplier, 2),
            "large_vessel_increment": lvi_charge,
            "oversize_supplement": oversize_charge,
            "zone_used": zone,
            "dimension_value": dim_value
        }

    # --- fixed_plus_variable (Algeciras, Valencia) ---
    elif pattern == "fixed_plus_variable":
        zone_key = invoice_line.get("zone_key") or invoice_line.get("terminal") or invoice_line.get("zone")
        if "rates" in profile:
            profile_rates = profile["rates"]
            if zone_key and zone_key in profile_rates:
                # Zone explicitly specified — use its rates directly
                rates_source = profile_rates[zone_key]
            elif zone_key:
                # Zone specified but not found — fall back to tariff file
                rates_source = tariff_data
            else:
                # No zone specified — use first (default) zone as base calculation.
                # Backward inference will probe all zones separately.
                first_zone = next(iter(profile_rates), None)
                rates_source = profile_rates[first_zone] if first_zone else tariff_data
        else:
            # Rates in tariff file — navigate to the right section/table
            tariff_matrix = tariff_data.get("rate_table") or {}
            if isinstance(tariff_matrix, dict) and profile.get("vessel_type_determines_table"):
                # First try: zone_key matches a table name directly (e.g. Huelva: "general_traffic")
                if zone_key and zone_key in tariff_matrix:
                    table = tariff_matrix[zone_key]
                else:
                    vessel_type = str(invoice_line.get("vessel_type", "general")).lower()
                    table_key = "gas_carrier_tariffs" if "gas" in vessel_type else "general_tariffs"
                    table = tariff_matrix.get(table_key) or next(iter(tariff_matrix.values()), {})
                rates_source = table.get("rates", []) if isinstance(table, dict) else tariff_data
            elif isinstance(tariff_matrix, dict) and zone_key and zone_key in tariff_matrix:
                # Generic zone-as-section-key (e.g. Cadiz Bay: geographic_sections_and_terminals)
                section = tariff_matrix[zone_key]
                if isinstance(section, dict):
                    rates_source = section.get("rates") or section.get("tariffs") or []
                else:
                    rates_source = section
            elif isinstance(tariff_matrix, list):
                rates_source = tariff_matrix
            else:
                rates_source = tariff_data
        return calculate_fixed_plus_variable(dim_value, rates_source, zone_key)
    # --- hp_hourly (Coatzacoalcos, Mazatlan) ---
    elif pattern == "hp_hourly":
        tug_hp_list = invoice_line.get("tug_hp_list") or [invoice_line.get("tug_hp", 0)]
        duration = _get_duration_hrs(sof_event, invoice_line)
        matrix = tariff_data.get("rate_table", [])
        hp_result = calculate_hp_hourly(
            grt_value=dim_value,
            tug_hp_list=[float(h) for h in tug_hp_list],
            actual_duration_hrs=duration,
            tariff_matrix=matrix,
            bow_thruster_discount=bool(invoice_line.get("bow_thruster_discount")),
            small_vessel_discount=bool(invoice_line.get("small_vessel_discount"))
        )
        fx_rate = float(invoice_line.get("fx_rate_mxn_usd") or 0)
        if fx_rate > 0:
            hp_result = dict(hp_result)
            hp_result["total_charge"] = round(hp_result["total_charge"] / fx_rate, 2)
            hp_result["fx_rate_applied"] = fx_rate
            hp_result["currency_note"] = "MXN charges converted to USD at invoice FX rate"
        return hp_result

    # --- hourly_with_minimum (Panama) ---
    elif pattern == "hourly_with_minimum":
        area_name = invoice_line.get("area") or invoice_line.get("zone", "")
        tariff_areas = tariff_data.get("tariff_a_port_towing", [])
        area_tariff = next(
            (a for a in tariff_areas if area_name.lower() in str(a.get("area", "")).lower()),
            tariff_areas[0] if tariff_areas else {}
        )
        duration = _get_duration_hrs(sof_event, invoice_line)
        return calculate_hourly_with_minimum(
            actual_duration_hrs=duration,
            area_tariff=area_tariff,
            tug_count=tug_count,
            rate_is_per_tug=profile.get("rate_is_per_tug", True) # Panama specific flag
        )

    # --- per_service_tug_count (Ensenada) ---
    elif pattern == "per_service_tug_count_specific":
        movement = invoice_line.get("movement_type", "arrival")
        duration = _get_duration_hrs(sof_event, invoice_line)
        matrix = tariff_data.get("rate_table", [])
        result = calculate_per_service_tug_count(
            trb_value=dim_value,
            tug_count=tug_count,
            movement_type=movement,
            actual_duration_hrs=duration,
            tariff_matrix=matrix,
            third_tug_requested=bool(invoice_line.get("third_tug_requested")),
            bow_thruster_discount=bool(invoice_line.get("bow_thruster_discount")),
            small_vessel_discount=bool(invoice_line.get("small_vessel_discount"))
        )
        # MXN → USD conversion (same as Guaymas/Manzanillo)
        fx_rate = float(invoice_line.get("fx_rate_mxn_usd") or 0)
        if fx_rate > 0:
            result = dict(result)
            for key in ("total_charge", "base_rate", "effective_rate", "overtime_charge", "third_tug_charge"):
                if result.get(key):
                    result[key] = round(result[key] / fx_rate, 2)
        return result

    # --- bracket_lookup_with_formula (Ceuta) ---
    elif pattern == "bracket_lookup_with_formula_above_threshold":
        table_id = invoice_line.get("tariff_table") or "T0"
        zone = invoice_line.get("zone", "zone_i_interior")
        tables = tariff_data.get("tariff_tables", [])
        return calculate_bracket_with_formula(
            gt_value=dim_value,
            table_id=table_id,
            tariff_tables=tables,
            zone=zone
        )

    else:
        raise ValueError(
            f"Unknown calculation pattern '{pattern}' for port '{port}'. "
            f"Check calculation_profiles.json."
        )


def _get_rates_data(tariff_data: dict, zone: Optional[str], route_hint: Optional[str] = None) -> Any:
    """
    Extracts the correct rates data structure for bracket_lookup.
    Handles zone-keyed dictionaries vs flat lists, including one level of nesting
    (e.g. Ghent: tariffs → tariff_a_operational_engine → terneuzen_ghent_area_south → [routes]).
    When a zone resolves to a list of route dicts, route_hint selects the right one.
    """
    tariffs = tariff_data.get("rate_table")

    def _pick_route_from_list(route_list: list) -> dict:
        """Given a list of route dicts, pick by route_hint or fall back to first."""
        if route_hint:
            hint_lower = route_hint.lower()
            for r in route_list:
                route_name = r.get("route", "").lower()
                if hint_lower in route_name or route_name in hint_lower:
                    return r.get("rates", {})
        return route_list[0].get("rates", {})

    if isinstance(tariffs, dict) and zone:
        zone_lower = zone.lower()

        def _is_route_list(lst: list) -> bool:
            """True if the list items represent Ghent-style {route, rates} dicts."""
            return bool(lst) and isinstance(lst[0], dict) and "route" in lst[0]

        # Level 1: direct key in tariffs
        for key in tariffs:
            if zone_lower in key.lower():
                val = tariffs[key]
                if isinstance(val, list) and val:
                    return _pick_route_from_list(val) if _is_route_list(val) else val
                return val

        # Level 2: zone nested inside a sub-table (e.g. tariff_a_operational_engine)
        for table_val in tariffs.values():
            if not isinstance(table_val, dict):
                continue
            for key in table_val:
                if zone_lower in key.lower():
                    val = table_val[key]
                    if isinstance(val, list) and val:
                        return _pick_route_from_list(val) if _is_route_list(val) else val
                    return val

        # Level 3: sections array where each item has an "id" field (Dordrecht/Moerdijk)
        sections = tariffs.get("sections")
        if isinstance(sections, list):
            for section in sections:
                if isinstance(section, dict) and section.get("id", "").lower() == zone_lower:
                    return section.get("rates", {})

        # Fallback: use first list found anywhere in tariffs
        for table_val in tariffs.values():
            if isinstance(table_val, list) and table_val:
                return _pick_route_from_list(table_val) if _is_route_list(table_val) else table_val
            if isinstance(table_val, dict):
                for sub_val in table_val.values():
                    if isinstance(sub_val, list) and sub_val:
                        return _pick_route_from_list(sub_val) if _is_route_list(sub_val) else sub_val

    # No zone: flat structures
    if isinstance(tariffs, dict):
        return tariffs.get("rates") or tariffs

    if isinstance(tariffs, list):
        return tariffs

    # Fallback: scan tariff_data for any list of bracket-like dicts.
    # Checks top level first (e.g. Antwerp: tariff_a_seagoing_vessels_operational_engine),
    # then one level deep (e.g. Rostock: tariff_a_base_rates.brackets).
    BRACKET_KEYS = {'dimension_range', 'dimension_min', 'dimension_max'}
    for val in tariff_data.values():
        if isinstance(val, list) and val and isinstance(val[0], dict):
            if BRACKET_KEYS & set(val[0].keys()):
                return val
    for val in tariff_data.values():
        if isinstance(val, dict):
            for sub_val in val.values():
                if isinstance(sub_val, list) and sub_val and isinstance(sub_val[0], dict):
                    if BRACKET_KEYS & set(sub_val[0].keys()):
                        return sub_val

    return {}


# ---------------------------------------------------------------------------
# MAIN ENTRY POINT
# ---------------------------------------------------------------------------

def route(
    port: str,
    sof_data: dict,
    invoice_lines: List[dict],
    tariff_data: dict,
    calculation_profiles: dict,
    invoice_reference: str = "",
    vendor: str = "Boluda",
    vessel_name: str = "",
    service_date: str = "",
    match_tolerance_pct: float = 1.0
) -> dict:
    """
    Main entry point. Routes an invoice through the full calculation pipeline.
    """
    profiles = calculation_profiles.get("calculation_profiles", calculation_profiles)
    profile = profiles.get(port)

    if profile is None:
        raise ValueError(
            f"Port '{port}' not found in calculation profiles. "
            f"Available: {list(profiles.keys())}"
        )

    currency = tariff_data.get("currency", "EUR").replace("EUR (€)", "EUR").replace("USD", "USD")
    dim_type = profile.get("dimension_type", "unknown")
    tug_count_source = profile.get("tug_count_source", "invoice")
    overtime_handler_key = profile.get("overtime_handler", "")
    # If overtime_handler is a dict (complex spec not yet implemented), treat as no handler
    if isinstance(overtime_handler_key, dict):
        overtime_handler_key = ""
    overtime_pattern = OVERTIME_PATTERN_MAP.get(overtime_handler_key, "pct_of_base_per_15min")
    
    has_open_doubts = port in calculation_profiles.get("ports_with_open_doubts", {})
    human_review_triggers = profile.get("requires_human_review_for", [])

    # Resolve which part of the SOF belongs to the invoice vessel
    # (handles multi-vessel SOFs where the primary vessel differs from the invoice vessel)
    effective_sof = _resolve_sof_for_vessel(sof_data, vessel_name)

    # Resolve Vessel Info from effective SOF
    _raw_vessel = effective_sof.get("vessel") or effective_sof.get("vessel_info") or {}
    vessel_info = _raw_vessel if isinstance(_raw_vessel, dict) else {}
    # Dim-type-aware: read the correct dimension key from the SOF vessel info
    if dim_type in ("GT",):
        _sof_raw_dim = vessel_info.get("gt") or vessel_info.get("loa")
    elif dim_type in ("GRT",):
        _sof_raw_dim = vessel_info.get("grt") or vessel_info.get("trb") or vessel_info.get("gt")
    else:  # LOA_meters, LOA, or unspecified
        _sof_raw_dim = vessel_info.get("loa") or vessel_info.get("gt") or vessel_info.get("grt")
    dim_value = _parse_vessel_dimension(_sof_raw_dim)
    if not vessel_name:
        vessel_name = vessel_info.get("name", "") or (
            _raw_vessel if isinstance(_raw_vessel, str) else ""
        )

    line_item_results = []

    for idx, invoice_line in enumerate(invoice_lines, start=1):
        service_type = str(
            invoice_line.get("service_type") or
            invoice_line.get("description") or "unknown"
        )
        invoiced_amount = float(invoice_line.get("amount") or invoice_line.get("total") or 0.0)

        # Inject vessel dimension into invoice_line if not present (for handlers that expect it there)
        # If the invoice line already carries a GT (injected by caller), use that and note any SOF mismatch.
        # _parse_vessel_dimension handles European format: "23.403" → 23403 GT.
        # Use dim_type-aware priority so a line with both loa and gt picks the right one.
        if dim_type in ("GT",):
            _inv_raw = invoice_line.get("gt") or invoice_line.get("loa")
        elif dim_type in ("GRT",):
            _inv_raw = invoice_line.get("grt") or invoice_line.get("trb") or invoice_line.get("gt")
        else:
            _inv_raw = invoice_line.get("loa") or invoice_line.get("gt") or invoice_line.get("grt")
        invoice_dim_value = _parse_vessel_dimension(_inv_raw)
        if invoice_dim_value:
            # Use the invoice's own dimension value for calculation
            dim_value_for_line = invoice_dim_value
        else:
            # Fall back to SOF vessel dimension and inject it
            dim_value_for_line = dim_value
            if dim_value:
                dim_key = dim_type.lower().replace("_meters", "")
                invoice_line[dim_key] = dim_value

        # GT mismatch check: if invoice carries its own GT and it differs from SOF GT by >1%
        gt_mismatch_note = ""
        if invoice_dim_value and dim_value and abs(invoice_dim_value - dim_value) / dim_value > 0.01:
            gt_mismatch_note = (
                f"GT on invoice ({invoice_dim_value:,.0f}) differs from SOF ({dim_value:,.0f}) "
                f"— calculation uses invoice value"
            )

        # Resolve SOF Event — only match events on the invoice line's service date
        sof_event = _resolve_sof_event(
            effective_sof, service_type,
            invoice_line.get("date_of_service") or invoice_line.get("date") or ""
        )
        sof_event_found = sof_event is not None
        sof_event_cited = (
            f"SOF event: {sof_event.get('type', service_type)} at "
            f"{sof_event.get('time') or sof_event.get('timestamp', 'unknown time')}"
            if sof_event else "No matching SOF event found"
        )

        # Resolve Tug Count
        tug_count = _get_tug_count(sof_event, invoice_line, tug_count_source)
        tug_spec_from_invoice = (tug_count_source == "invoice")

        # Human Review Check
        human_review_flag = False
        human_review_reason = ""
        for trigger in human_review_triggers:
            if trigger.lower() in service_type.lower():
                human_review_flag = True
                human_review_reason = f"Human review required: {trigger}"
                break

        # Append GT mismatch note to human review reason if present
        if gt_mismatch_note:
            human_review_flag = True
            human_review_reason = (
                f"{human_review_reason}; {gt_mismatch_note}" if human_review_reason
                else gt_mismatch_note
            )

        # Zone Inference Flag
        zone_inferred = (
            invoice_line.get("zone") is None and
            profile.get("zone_determines_rate", False)
        )

        try:
            # 1. Handler
            handler_result = _dispatch_handler(
                port=port,
                profile=profile,
                tariff_data=tariff_data,
                invoice_line=invoice_line,
                sof_event=sof_event,
                tug_count=tug_count
            )

            tariff_rule_cited = (
                f"{port} tariff — pattern: {profile.get('calculation_pattern')}, "
                f"dim: {dim_type}={dim_value_for_line}, "
                f"zone: {invoice_line.get('zone', 'N/A')}"
            )

            # 2. Surcharges
            surcharge_list = _build_surcharge_list(invoice_line, profile, port)
            surcharge_report = None
            if surcharge_list:
                # Use base_rate if available (raw calc), otherwise total_charge
                base_for_surcharges = float(
                    handler_result.get("base_rate") or
                    handler_result.get("total_charge") or
                    handler_result.get("total_rate") or 0.0
                )
                surcharge_report = apply_surcharges(
                    base_rate=base_for_surcharges,
                    applicable_surcharges=surcharge_list,
                    port=port
                )

            # 3. Overtime Calculation
            overtime_result = None

            # Handlers that manage overtime internally should not trigger this step.
            # Also skip if no overtime_handler is declared in the profile (port doesn't bill overtime).
            skip_overtime_patterns = {"hp_hourly", "hourly_with_minimum", "per_service_tug_count_specific"}

            if overtime_handler_key and profile.get("calculation_pattern") not in skip_overtime_patterns:
                actual_duration = _get_duration_hrs(sof_event, invoice_line)
                standard_duration = float(
                    tariff_data.get("billing_logic_definitions", {})
                    .get("duration_logic", {})
                    .get("standard_included_duration_hrs", 1.0)
                )

                # Prepare kwargs based on pattern type
                ot_kwargs = {}
                
                if overtime_pattern == "flat_hourly_per_tug":
                    # Pattern 1: Flat hourly rate per tug (Ghent, Antwerp, Rotterdam)
                    if port in OVERTIME_RATES:
                        ot_kwargs = OVERTIME_RATES[port].copy()
                    else:
                        logger.warning(f"Port {port} uses flat_hourly_per_tug but no rate in OVERTIME_RATES")
                    ot_kwargs["tug_count"] = tug_count  # only this pattern needs tug_count
                else:
                    # Pattern 2–6: Percentage/prorata based on calculated rate
                    base_for_overtime = float(
                        handler_result.get("base_rate") or
                        handler_result.get("total_rate") or 0.0
                    )
                    ot_kwargs["base_rate"] = base_for_overtime

                    if overtime_pattern == "prorata_10min_rounding":
                        # prorata_10min_rounding uses first_hour_rate_per_tug + tug_count
                        ot_kwargs.pop("base_rate", None)
                        ot_kwargs["first_hour_rate_per_tug"] = base_for_overtime
                        ot_kwargs["tug_count"] = tug_count
                    elif overtime_pattern == "fixed_hourly_amount":
                        # fixed_hourly_amount uses a fixed rate (not % of base) + tug_count
                        ot_kwargs.pop("base_rate", None)
                        ot_kwargs["hourly_rate"] = float(profile.get("overtime_hourly_rate", 1560.89))
                        ot_kwargs["tug_count"] = tug_count
                    elif "overtime_surcharge_pct" in profile:
                        ot_kwargs["surcharge_pct"] = profile["overtime_surcharge_pct"]
                    elif overtime_pattern == "pct_surcharge_on_assist_rate":
                        ot_kwargs["surcharge_pct"] = 30.0
                        ot_kwargs["assist_rate"] = base_for_overtime
                        ot_kwargs.pop("base_rate", None)  # function takes assist_rate, not base_rate

                ot_kwargs["port"] = port
                
                overtime_result = calculate_overtime(
                    pattern=overtime_pattern,
                    actual_duration_hrs=actual_duration,
                    standard_duration_hrs=standard_duration,
                    **ot_kwargs
                )

            # 4. Backward inference — find candidate explanations for any variance
            known_surcharge_types = [s["type"] for s in surcharge_list]
            candidate_explanations = _infer_plausible_conditions(
                profile=profile,
                dim_value=dim_value_for_line,
                invoiced_amount=invoiced_amount,
                known_surcharge_types=known_surcharge_types
            )

            # 5. Build Line Item
            line_result = build_line_item(
                line_number=idx,
                service_description=service_type,
                invoiced_amount=invoiced_amount,
                currency=currency,
                handler_result=handler_result,
                sof_event_cited=sof_event_cited,
                tariff_rule_cited=tariff_rule_cited,
                handler_used=profile.get("calculation_pattern", "unknown"),
                surcharge_report=surcharge_report,
                overtime_result=overtime_result,
                human_review_flag=human_review_flag,
                human_review_reason=human_review_reason,
                sof_event_found=sof_event_found,
                exact_tariff_match=True,
                has_open_doubts=has_open_doubts,
                tug_spec_from_invoice=tug_spec_from_invoice,
                zone_inferred=zone_inferred,
                candidate_explanations=candidate_explanations,
                match_tolerance_pct=match_tolerance_pct
            )

        except Exception as e:
            logger.exception(f"Port '{port}' line {idx} ('{service_type}') calculation failed.")
            # Return a failed line item so the whole invoice doesn't fail
            line_result = build_line_item(
                line_number=idx,
                service_description=service_type,
                invoiced_amount=invoiced_amount,
                currency=currency,
                handler_result={"base_rate": 0.0, "total_rate": 0.0, "error": str(e)},
                sof_event_cited=sof_event_cited,
                tariff_rule_cited=f"CALCULATION_ERROR: {str(e)}",
                handler_used="error",
                sof_event_found=sof_event_found,
                exact_tariff_match=False,
                has_open_doubts=True,
                human_review_flag=True,
                human_review_reason=f"System Error: {str(e)}",
                match_tolerance_pct=match_tolerance_pct
            )

        line_item_results.append(line_result)

    # 5. Assemble Full Result
    result = build_result(
        invoice_reference=invoice_reference,
        vendor=vendor,
        port=port,
        vessel_name=vessel_name,
        vessel_dimension_type=dim_type,
        vessel_dimension_value=dim_value,
        service_date=service_date,
        line_items=line_item_results,
        currency=currency
    )

    return to_dict(result)
