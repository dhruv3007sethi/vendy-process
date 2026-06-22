
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
import re
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

from .handlers.bracket_lookup import (
    lookup_bracket_rate,
    calculate_oversize_supplement,
    calculate_large_vessel_increment
)
from .handlers.fixed_plus_variable import calculate_fixed_plus_variable
from .handlers.hp_hourly import calculate_hp_hourly
from .handlers.hourly_with_minimum import calculate_hourly_with_minimum
from .handlers.per_service_tug_count import calculate_per_service_tug_count
from .handlers.bracket_lookup_with_formula import calculate_bracket_with_formula
from .surcharge_engine import apply_surcharges
from .overtime_calculator import calculate_overtime
from .output_formatter import build_line_item, build_result, to_dict, LineItemResult
logger = logging.getLogger(__name__)


class ZoneUnresolvableError(ValueError):
    """Raised when a port has multiple rate columns but the invoice zone string
    cannot be mapped to any known tariff area."""


# ---------------------------------------------------------------------------
# ADJUSTMENT LINE AUTO-DETECTION
# ---------------------------------------------------------------------------

# Keywords whose presence in service_type indicates a non-tariff adjustment
# (holiday/weekend surcharges, contractual discounts, bunker/fuel adjustments).
# Lines matching these are accepted on face value and assigned verdict ADJUSTMENT
# rather than being run through the tariff calculator.
_ADJUSTMENT_KEYWORDS: frozenset = frozenset([
    "holiday",
    "weekend",
    "public holiday",
    "discount",
    "rebate",
    "bunker",
    "fuel surcharge",
    "fuel adj",
    "bunker adj",
])


def _classify_as_adjustment(service_type: str, description: str = "") -> str:
    """
    Returns the matched keyword if the line is a non-tariff adjustment,
    or an empty string if it should be validated against the tariff.
    Checks both service_type and description — invoices often carry the
    keyword only in the description (e.g. service_type='Unberth', description='HOLIDAY').
    """
    for text in (service_type.lower(), description.lower()):
        for kw in _ADJUSTMENT_KEYWORDS:
            if kw in text:
                return kw
    return ""


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
        # ±7 day tolerance: handles (a) late-night maneuvers logged next calendar day,
        # and (b) vessels waiting at anchor several days before berthing — where the
        # SOF "Berth" event may be dated to the vessel's arrival at anchorage/pilot
        # station (E.O.S.P.) rather than the actual tug-assist date.
        try:
            svc_dt = datetime.strptime(service_date, "%Y-%m-%d")
            evt_dt = datetime.strptime(event_date, "%Y-%m-%d")
            return abs((evt_dt - svc_dt).days) <= 7
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


def _get_duration_hrs(sof_event: Optional[dict], invoice_line: dict) -> tuple[float, bool]:
    """
    Returns (actual_service_duration_hrs, defaulted).

    Prefers SOF event duration; falls back to invoice.
    If neither source provides a value, returns (1.0, True).
    """
    if sof_event:
        duration = sof_event.get("duration_hrs") or sof_event.get("duration_hours")
        if duration is not None:
            return float(duration), False

    inv_duration = invoice_line.get("duration_hrs") or invoice_line.get("duration_hours")
    if inv_duration is not None:
        return float(inv_duration), False

    return 1.0, True


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
        mult = multipliers.get("fog_visibility", 1.5)
        surcharges.append({"type": "fog_visibility", "multiplier": mult})

    if invoice_line.get("lng_vessel"):
        mult = multipliers.get("lng_vessel", 2.5)
        surcharges.append({"type": "lng_vessel", "multiplier": mult})

    if invoice_line.get("deep_draft"):
        mult = multipliers.get("deep_draft", 1.5)
        surcharges.append({"type": "deep_draft", "multiplier": mult})

    if invoice_line.get("shifting"):
        mult = multipliers.get("shifting", 1.3)
        surcharges.append({
            "type": "shifting", 
            "multiplier": mult,
            "shift_type": invoice_line.get("shift_type", "intra_area")
        })

    if invoice_line.get("dry_dock"):
        mult = multipliers.get("dry_dock", 1.5)
        surcharges.append({"type": "dry_dock", "multiplier": mult})

    if invoice_line.get("late_order"):
        mult = multipliers.get("late_order", 2.0)
        surcharges.append({"type": "late_order", "multiplier": mult})

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


def _zone_variants_for_profile(
    profile: dict,
    tariff_data: dict,
    dim_value: float,
    tug_count: int,
) -> List[tuple]:
    """
    Yield (zone_label, total_rate) pairs for every zone the port's tariff
    can produce.  Dispatches to the correct handler based on calculation_pattern.
    """
    pattern = profile.get("calculation_pattern", "")
    results: List[tuple] = []

    if pattern == "fixed_plus_variable":
        for zone, zone_rates in profile.get("rates", {}).items():
            if not isinstance(zone_rates, dict):
                continue
            try:
                r = calculate_fixed_plus_variable(dim_value, zone_rates, zone)
                results.append((zone, round(float(r.get("total_rate") or 0.0), 2)))
            except Exception as e:
                logger.debug(f"Backward inference: fixed_plus_variable zone '{zone}' failed: {e}")

    elif pattern == "bracket_lookup":
        zone_rate_columns = profile.get("zone_rate_columns", {})
        billing_basis = profile.get("billing_basis", "per_tug_per_move")
        tug_multiplier = tug_count if billing_basis == "per_tug_per_move" else 1
        # Deduplicate: multiple zone names can map to the same rate column
        seen_columns: dict = {}
        for zone_name, rate_col in zone_rate_columns.items():
            if rate_col in seen_columns:
                continue
            seen_columns[rate_col] = zone_name
            try:
                rates_data = _get_rates_data(tariff_data, zone_name)
                base = lookup_bracket_rate(rates_data, dim_value, rate_col)
                total = round(base * tug_multiplier, 2)
                results.append((zone_name, total))
            except Exception as e:
                logger.debug(f"Backward inference: bracket_lookup zone '{zone_name}' col '{rate_col}' failed: {e}")

    elif pattern == "bracket_lookup_with_formula_above_threshold":
        tables = tariff_data.get("tariff_tables", [])
        table_id = "T0"
        for zone in ("zone_i_interior", "zone_ii_exterior"):
            try:
                r = calculate_bracket_with_formula(dim_value, table_id, tables, zone)
                results.append((zone, round(float(r.get("total_rate") or 0.0), 2)))
            except Exception as e:
                logger.debug(f"Backward inference: bracket_with_formula zone '{zone}' failed: {e}")

    return results


def _infer_plausible_conditions(
    profile: dict,
    tariff_data: dict,
    dim_value: float,
    tug_count: int,
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
    profile_surcharges = profile.get("surcharge_multipliers", {})
    waiting = profile.get("waiting_time", {})
    delay_bonuses = profile.get("provider_delay_bonuses", {})

    all_surcharges = {**_SURCHARGE_ENGINE_DEFAULTS, **profile_surcharges}
    unknown_surcharges = {
        k: v for k, v in all_surcharges.items()
        if k not in known_surcharge_types
    }

    def _variance(expected: float) -> float:
        return round(abs(invoiced_amount - expected) / invoiced_amount * 100, 2)

    def _within(variance: float) -> bool:
        return variance <= plausible_tolerance_pct

    # --- Compute zone variants using the correct handler ---
    zone_rates = _zone_variants_for_profile(profile, tariff_data, dim_value, tug_count)

    # --- Level 1: Zone variants ---
    for zone, expected in zone_rates:
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

    # Compute base using the first (default/fallback) zone for Levels 2–5
    base_rate = zone_rates[0][1] if zone_rates else 0.0

    if base_rate > 0:
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
        for zone, zone_base in zone_rates:
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
                continue
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
    if any(c["variance_pct"] == 0.0 for c in candidates):
        candidates = [c for c in candidates if c["variance_pct"] == 0.0]

    return candidates


# ---------------------------------------------------------------------------
# CONTRACT DISCOUNT RESOLUTION
# ---------------------------------------------------------------------------

def _resolve_zone_key(zone_input: Optional[str], zone_rate_columns: dict) -> tuple[Optional[str], bool]:
    """
    Resolves a free-text zone string from an invoice line to a canonical zone key
    used in zone_rate_columns.

    Strategy (ranked — first decisive tier wins):
      1. Exact match (case-sensitive) — zone_inferred=False
      2. Exact match (case-insensitive) — zone_inferred=False
      3. Substring match — all candidates ranked by overlap ratio, zone_inferred=True
      4. Token-level substring — ranked by (token_count, total_chars), zone_inferred=True
      5. No match — return None, zone_inferred=False

    Returns (resolved_key, zone_inferred).
    """
    if not zone_input or not zone_rate_columns:
        return zone_input, False
    # 1. Exact match (case-sensitive)
    if zone_input in zone_rate_columns:
        return zone_input, False

    zone_lower = zone_input.lower()

    # 2. Exact match (case-insensitive)
    for key in zone_rate_columns:
        if key.lower() == zone_lower:
            return key, False

    # 3. Substring match: collect all, rank by overlap ratio (closest to exact wins)
    matches = []
    for key in zone_rate_columns:
        key_lower = key.lower()
        if key_lower in zone_lower or zone_lower in key_lower:
            shorter = min(len(key_lower), len(zone_lower))
            longer = max(len(key_lower), len(zone_lower))
            ratio = shorter / longer if longer else 0.0
            matches.append((key, ratio))
    if matches:
        matches.sort(key=lambda m: m[1], reverse=True)
        best_key = matches[0][0]
        if len(matches) > 1:
            logger.warning(
                "Zone '%s' matched multiple zone keys: %s; using best match '%s'",
                zone_input, [m[0] for m in matches], best_key,
            )
        else:
            logger.info("Zone '%s' fuzzy-matched to '%s' (zone_inferred=True)", zone_input, best_key)
        return best_key, True

    # 4. Token-level substring: rank by (matching_token_count, total_matched_chars)
    tokens = [t for t in re.split(r'[^a-z0-9]+', zone_lower) if len(t) >= 4]
    if tokens:
        tok_matches = []
        for key in zone_rate_columns:
            key_lower = key.lower()
            matched = [t for t in tokens if t in key_lower]
            if matched:
                tok_matches.append((key, len(matched), sum(len(t) for t in matched)))
        if tok_matches:
            tok_matches.sort(key=lambda m: (m[1], m[2]), reverse=True)
            best_key = tok_matches[0][0]
            if len(tok_matches) > 1:
                logger.warning(
                    "Zone '%s' token-matched multiple zone keys: %s; using best match '%s'",
                    zone_input, [m[0] for m in tok_matches], best_key,
                )
            else:
                logger.info("Zone '%s' token-matched to '%s' (zone_inferred=True)", zone_input, best_key)
            return best_key, True

    # 5. No match
    return None, False


def _resolve_zone_from_tariff_areas(
    zone_input: str,
    tariff_data: dict,
    zone_rate_columns: dict
) -> tuple[Optional[str], bool]:
    """
    Secondary zone resolution: scan tariff operating area definitions for any
    area key or terminal/description text that substring-matches zone_input.

    Handles two common tariff formats:
      - Rotterdam: operating_areas_and_terminals → {area: {terminals: [...], ...}}
      - Antwerp:   operating_areas              → {area: {description: "...", ...}}

    All candidates are collected and ranked by (matching_token_count, total_matched_chars).
    A warning is logged when multiple areas match.

    Returns (canonical_zone_key, zone_inferred=True) or (None, False).
    Only returns a key that exists in zone_rate_columns.
    """
    if not zone_input or not zone_rate_columns:
        return None, False

    zone_lower = zone_input.lower()
    tokens = [t for t in re.split(r'[^a-z0-9]+', zone_lower) if len(t) >= 4]
    if not tokens:
        return None, False

    matches = []
    for field in ("operating_areas_and_terminals", "operating_areas"):
        areas = tariff_data.get(field, {})
        if not isinstance(areas, dict):
            continue
        for area_key, area_data in areas.items():
            if area_key not in zone_rate_columns:
                continue
            if not isinstance(area_data, dict):
                continue
            corpus_parts = [t.lower() for t in area_data.get("terminals", [])]
            desc = area_data.get("description", "")
            if desc:
                corpus_parts.append(desc.lower())
            corpus = " ".join(corpus_parts)
            matched = [t for t in tokens if t in corpus]
            if matched:
                matches.append((area_key, field, len(matched), sum(len(t) for t in matched)))

    if matches:
        matches.sort(key=lambda m: (m[2], m[3]), reverse=True)
        best_key, best_field = matches[0][0], matches[0][1]
        if len(matches) > 1:
            logger.warning(
                "Zone '%s' matched multiple tariff areas: %s; using best match '%s'",
                zone_input, [m[0] for m in matches], best_key,
            )
        else:
            logger.info(
                "Zone '%s' token-matched tariff area '%s' via %s lookup (zone_inferred=True)",
                zone_input, best_key, best_field,
            )
        return best_key, True

    return None, False


def _resolve_zone_from_description(
    invoice_line: dict,
    zone_keys: dict,
    tariff_data: dict = None,
) -> tuple[Optional[str], bool]:
    """
    Fallback zone resolution: scan the invoice line's *description* field for
    zone clues when the zone field itself could not be resolved.

    Reuses _resolve_zone_key (substring/token matching against zone_keys dict
    keys) and optionally _resolve_zone_from_tariff_areas (operating-area
    description scan).

    Returns (resolved_key, zone_inferred=True) or (None, False).
    """
    desc = invoice_line.get("description") or ""
    if not desc or not zone_keys:
        return None, False

    zone, _ = _resolve_zone_key(desc, zone_keys)
    if zone:
        logger.info(
            "Zone resolved from line description via zone-key match: '%s' (from '%s')",
            zone, desc,
        )
        return zone, True

    if tariff_data:
        zone, _ = _resolve_zone_from_tariff_areas(desc, tariff_data, zone_keys)
        if zone:
            logger.info(
                "Zone resolved from line description via tariff-area match: '%s' (from '%s')",
                zone, desc,
            )
            return zone, True

    return None, False


def _resolve_contract_discount_pct(profile: dict, service_date: str) -> float:
    """
    Returns the flat contract discount % from profile["contract_discount"]["pct"].
    Returns 0.0 if no contract discount is configured.
    """
    cd = profile.get("contract_discount")
    if not cd:
        return 0.0
    return float(cd.get("pct", 0.0))


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
    # Do NOT fall back across dimension types (LOA must not be used as GT).
    if dim_type in ("GT",):
        _raw_dim = invoice_line.get("gt")
    elif dim_type in ("GRT",):
        _raw_dim = invoice_line.get("grt") or invoice_line.get("trb")
    else:  # LOA_meters, LOA, or unspecified
        _raw_dim = invoice_line.get("loa")
    dim_value = _parse_vessel_dimension(_raw_dim)

    # --- bracket_lookup (Ghent, Antwerp, etc.) ---
    if pattern == "bracket_lookup":
        zone_raw = invoice_line.get("zone") or invoice_line.get("route")
        route_hint = invoice_line.get("route_hint")
        zone_rate_columns = profile.get("zone_rate_columns", {})
        # Pass 1: match against zone_rate_columns keys (exact / substring / token)
        zone, _zone_inferred = _resolve_zone_key(zone_raw, zone_rate_columns)
        # Pass 2: if still unresolved, scan tariff operating_areas / operating_areas_and_terminals
        if zone is None and zone_raw and zone_rate_columns:
            zone, _zone_inferred = _resolve_zone_from_tariff_areas(zone_raw, tariff_data, zone_rate_columns)
        # Pass 3: if still unresolved, try the invoice line description for zone clues
        if zone is None and zone_rate_columns:
            zone, _zone_inferred = _resolve_zone_from_description(invoice_line, zone_rate_columns, tariff_data)
        # Write resolved zone back so tariff_rule_cited shows the canonical key
        invoice_line["zone"] = zone
        invoice_line["_zone_inferred"] = _zone_inferred
        rates_data = _get_rates_data(tariff_data, zone, route_hint)
        # Zone-specific rate column (e.g. Antwerp: zandvliet_kallo_rate_eur vs antwerp_city_rate_eur)
        rate_col = zone_rate_columns.get(zone) if zone else None
        # Guard: if this port has multiple rate columns but zone is still unresolved, raise a
        # clear human-readable error rather than a cryptic "no rate field found" from the handler.
        if rate_col is None and zone_rate_columns:
            known = [k for k in zone_rate_columns if k == k.upper() and len(k) > 3]
            if not known:
                known = list(zone_rate_columns.keys())
            label = zone_raw or "(not provided)"
            raise ZoneUnresolvableError(
                f"Zone '{label}' could not be mapped to a known tariff area "
                f"({' / '.join(known)}). Confirm the correct area and resubmit."
            )
        base_rate = lookup_bracket_rate(rates_data, dim_value, rate_col)

        # MXN → USD conversion (Guaymas and similar Mexican bracket ports)
        try:
            fx_rate = float(invoice_line.get("fx_rate_mxn_usd") or 0)
        except (ValueError, TypeError):
            raise ValueError(f"Non-numeric FX rate: {invoice_line.get('fx_rate_mxn_usd')!r}")
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
            "total_rate": round(base_rate * tug_multiplier + lvi_charge + oversize_charge, 2),
            "large_vessel_increment": lvi_charge,
            "oversize_supplement": oversize_charge,
            "zone_used": zone,
            "dimension_value": dim_value
        }

    # --- fixed_plus_variable (Algeciras, Valencia) ---
    elif pattern == "fixed_plus_variable":
        zone_key = (invoice_line.get("zone_key") or invoice_line.get("terminal")
                    or invoice_line.get("zone") or profile.get("default_zone"))
        if "rates" in profile:
            profile_rates = profile["rates"]
            if zone_key and zone_key in profile_rates:
                # Zone explicitly specified — use its rates directly
                rates_source = profile_rates[zone_key]
            elif zone_key:
                # Zone specified but not found — try description fallback
                desc_zone, _ = _resolve_zone_from_description(invoice_line, profile_rates)
                if desc_zone and desc_zone in profile_rates:
                    zone_key = desc_zone
                    invoice_line["zone"] = zone_key
                    invoice_line["_zone_inferred"] = True
                    rates_source = profile_rates[zone_key]
                else:
                    rates_source = tariff_data
            else:
                # No zone specified — try description fallback before defaulting
                desc_zone, _ = _resolve_zone_from_description(invoice_line, profile_rates)
                if desc_zone and desc_zone in profile_rates:
                    zone_key = desc_zone
                    invoice_line["zone"] = zone_key
                    invoice_line["_zone_inferred"] = True
                    rates_source = profile_rates[zone_key]
                else:
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
            elif isinstance(tariff_matrix, dict) and zone_key:
                # Generic zone-as-section-key (e.g. Cadiz Bay: geographic_sections_and_terminals)
                effective_zone = zone_key
                if zone_key not in tariff_matrix:
                    # Zone specified but not found — try description fallback
                    section_keys = {k: k for k in tariff_matrix if isinstance(tariff_matrix[k], (dict, list))}
                    desc_zone, _ = _resolve_zone_from_description(invoice_line, section_keys)
                    effective_zone = desc_zone if desc_zone and desc_zone in tariff_matrix else None
                    if effective_zone:
                        zone_key = effective_zone
                        invoice_line["zone"] = zone_key
                        invoice_line["_zone_inferred"] = True
                if effective_zone and effective_zone in tariff_matrix:
                    section = tariff_matrix[effective_zone]
                    if isinstance(section, dict):
                        rates_source = section.get("rates") or section.get("tariffs") or []
                    else:
                        rates_source = section
                else:
                    rates_source = tariff_data
            elif isinstance(tariff_matrix, list):
                rates_source = tariff_matrix
            else:
                rates_source = tariff_data
        return calculate_fixed_plus_variable(dim_value, rates_source, zone_key)
    # --- hp_hourly (Coatzacoalcos, Mazatlan) ---
    elif pattern == "hp_hourly":
        tug_hp_list = invoice_line.get("tug_hp_list") or [invoice_line.get("tug_hp", 0)]
        duration, duration_defaulted = _get_duration_hrs(sof_event, invoice_line)
        if duration_defaulted:
            logger.warning("hp_hourly: duration missing from SOF and invoice — defaulting to 1.0 hr")
        matrix = tariff_data.get("rate_table", [])
        hp_result = calculate_hp_hourly(
            grt_value=dim_value,
            tug_hp_list=[float(h) for h in tug_hp_list],
            actual_duration_hrs=duration,
            tariff_matrix=matrix,
            bow_thruster_discount=bool(invoice_line.get("bow_thruster_discount")),
            small_vessel_discount=bool(invoice_line.get("small_vessel_discount"))
        )
        try:
            fx_rate = float(invoice_line.get("fx_rate_mxn_usd") or 0)
        except (ValueError, TypeError):
            raise ValueError(f"Non-numeric FX rate: {invoice_line.get('fx_rate_mxn_usd')!r}")
        if fx_rate > 0:
            hp_result = dict(hp_result)
            hp_result["total_rate"] = round(hp_result["total_rate"] / fx_rate, 2)
            hp_result["total_charge"] = hp_result["total_rate"]
            hp_result["fx_rate_applied"] = fx_rate
            hp_result["currency_note"] = "MXN charges converted to USD at invoice FX rate"
        if duration_defaulted:
            hp_result = dict(hp_result) if not isinstance(hp_result, dict) else hp_result
            hp_result["_duration_defaulted"] = True
        return hp_result

    # --- hourly_with_minimum (Panama) ---
    elif pattern == "hourly_with_minimum":
        area_name = invoice_line.get("area") or invoice_line.get("zone", "")
        tariff_areas = tariff_data.get("tariff_a_port_towing", [])
        area_tariff = next(
            (a for a in tariff_areas if area_name.lower() in str(a.get("area", "")).lower()),
            tariff_areas[0] if tariff_areas else {}
        )
        duration, duration_defaulted = _get_duration_hrs(sof_event, invoice_line)
        if duration_defaulted:
            logger.warning("hourly_with_minimum: duration missing from SOF and invoice — defaulting to 1.0 hr")
        result = calculate_hourly_with_minimum(
            actual_duration_hrs=duration,
            area_tariff=area_tariff,
            tug_count=tug_count,
            rate_is_per_tug=profile.get("rate_is_per_tug", True)
        )
        if duration_defaulted:
            result = dict(result) if not isinstance(result, dict) else result
            result["_duration_defaulted"] = True
        return result

    # --- per_service_tug_count (Ensenada) ---
    elif pattern == "per_service_tug_count_specific":
        movement = invoice_line.get("movement_type", "arrival")
        duration, duration_defaulted = _get_duration_hrs(sof_event, invoice_line)
        if duration_defaulted:
            logger.warning("per_service_tug_count: duration missing from SOF and invoice — defaulting to 1.0 hr")
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
        try:
            fx_rate = float(invoice_line.get("fx_rate_mxn_usd") or 0)
        except (ValueError, TypeError):
            raise ValueError(f"Non-numeric FX rate: {invoice_line.get('fx_rate_mxn_usd')!r}")
        if fx_rate > 0:
            result = dict(result)
            for key in ("total_rate", "total_charge", "base_rate", "effective_rate", "overtime_charge", "third_tug_charge"):
                if result.get(key):
                    result[key] = round(result[key] / fx_rate, 2)
        if duration_defaulted:
            result = dict(result) if not isinstance(result, dict) else result
            result["_duration_defaulted"] = True
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

    # --- concession_check_only (no tariff data available; only discount verification) ---
    elif pattern == "concession_check_only":
        return {"base_rate": 0.0, "total_rate": 0.0, "concession_check_only": True}

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
# MULTI-DATE MERGE
# ---------------------------------------------------------------------------

_VERDICT_RANK = {"AUTO_APPROVED": 0, "MISMATCH": 1, "REVIEW_REQUIRED": 2}


def _merge(results: List[dict]) -> dict:
    """
    Merge per-date _route_single_date() results into one response dict.

    Verdict  : worst across groups (REVIEW_REQUIRED > MISMATCH > AUTO_APPROVED)
    Confidence: min across groups
    Financials: summed
    Lines    : concatenated and renumbered 1…N
    """
    if len(results) == 1:
        return results[0]

    all_lines = []
    for r in results:
        all_lines.extend(r.get("line_items", []))
    for i, line in enumerate(all_lines, start=1):
        line["line_number"] = i

    overall_verdict = max(
        (r.get("overall_verdict", "AUTO_APPROVED") for r in results),
        key=lambda v: _VERDICT_RANK.get(v, 0),
    )

    overall_confidence = min(r.get("overall_confidence", 1.0) for r in results)
    if overall_confidence >= 0.90:
        conf_label = "HIGH"
    elif overall_confidence >= 0.70:
        conf_label = "MEDIUM"
    else:
        conf_label = "LOW"

    first = results[0]
    return {
        "invoice_reference":      first.get("invoice_reference", ""),
        "vendor":                 first.get("vendor", ""),
        "port":                   first.get("port", ""),
        "vessel_name":            first.get("vessel_name", ""),
        "vessel_dimension_type":  first.get("vessel_dimension_type", ""),
        "vessel_dimension_value": first.get("vessel_dimension_value", 0.0),
        "service_date":           first.get("service_date", ""),
        "currency":               first.get("currency", "EUR"),
        "validated_at":           first.get("validated_at", ""),
        "overall_verdict":            overall_verdict,
        "overall_confidence":         round(overall_confidence, 4),
        "overall_confidence_label":   conf_label,
        "human_review_required":      any(r.get("human_review_required") for r in results),
        "total_expected":         round(sum(r.get("total_expected",        0.0) for r in results), 2),
        "total_invoiced":         round(sum(r.get("total_invoiced",        0.0) for r in results), 2),
        "total_variance":         round(sum(r.get("total_variance",        0.0) for r in results), 2),
        "fuel_surcharge_total":   round(sum(r.get("fuel_surcharge_total",  0.0) for r in results), 2),
        "invoice_amount_gross":   round(sum(r.get("invoice_amount_gross",  0.0) for r in results), 2),
        "invoice_amount_net":     round(sum(r.get("invoice_amount_net",    0.0) for r in results), 2),
        "summary_notes": [note for r in results for note in r.get("summary_notes", [])],
        "line_items":    all_lines,
    }


# ---------------------------------------------------------------------------
# MAIN ENTRY POINT
# ---------------------------------------------------------------------------

def _route_single_date(
    port: str,
    sof_data: dict,
    invoice_lines: List[dict],
    tariff_data: dict,
    calculation_profiles: dict,
    invoice_reference: str = "",
    vendor: str = "Boluda",
    vessel_name: str = "",
    service_date: str = "",
    match_tolerance_pct: float = 1.0,
    adjustment_lines: List[dict] = None,
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
    # Dim-type-aware: read the correct dimension key from the SOF vessel info.
    # Do NOT fall back across dimension types (LOA must not be used as GT).
    if dim_type in ("GT",):
        _sof_raw_dim = vessel_info.get("gt")
    elif dim_type in ("GRT",):
        _sof_raw_dim = vessel_info.get("grt") or vessel_info.get("trb")
    else:  # LOA_meters, LOA, or unspecified
        _sof_raw_dim = vessel_info.get("loa")
    dim_value = _parse_vessel_dimension(_sof_raw_dim)
    if not vessel_name:
        vessel_name = vessel_info.get("name", "") or (
            _raw_vessel if isinstance(_raw_vessel, str) else ""
        )

    line_item_results = []

    # Detect separately-billed overtime lines (e.g. Las Palmas "Servicios especiales").
    # When present, suppress internal OT calculation on Berth/Unberth lines to avoid
    # double-counting — OT is validated on its own dedicated line(s) instead.
    separate_ot_lines = any(
        str(l.get("service_type") or "").lower() == "overtime"
        for l in invoice_lines
    )
    num_ot_lines = sum(
        1 for l in invoice_lines
        if str(l.get("service_type") or "").lower() == "overtime"
    )

    for idx, invoice_line in enumerate(invoice_lines, start=1):
        service_type = str(
            invoice_line.get("service_type") or
            invoice_line.get("description") or "unknown"
        )
        invoiced_amount = float(invoice_line.get("amount") or invoice_line.get("total") or 0.0)

        # Auto-detect adjustment/surcharge lines that are not tariff-validated services.
        # These bypass the tariff calculator and are accepted on face value.
        _adj_kw = _classify_as_adjustment(
            service_type,
            invoice_line.get("description") or ""
        )
        if _adj_kw:
            logger.info(
                f"Line {idx} ({service_type}): auto-classified as ADJUSTMENT "
                f"(matched keyword '{_adj_kw}') — accepted on face value"
            )
            line_item_results.append(LineItemResult(
                line_number=idx,
                service_description=service_type,
                sof_event_cited="N/A — adjustment/surcharge line",
                tariff_rule_cited=f"Auto-detected non-tariff line (keyword: '{_adj_kw}') — accepted on face value",
                expected_amount=invoiced_amount,
                invoiced_amount=invoiced_amount,
                currency=currency,
                variance=0.0,
                variance_pct=0.0,
                verdict="ADJUSTMENT",
                confidence_score=1.0,
                confidence_label="HIGH",
                handler_used="adjustment",
                surcharges_applied=[],
                overtime_applied=None,
                human_review_flag=False,
                human_review_reason="",
                notes=f"Auto-classified as adjustment/surcharge — not validated against tariff",
            ))
            continue

        # Initialise review flags early — dimension validation below may set them
        human_review_flag = False
        human_review_reason = ""

        # Inject vessel dimension into invoice_line if not present (for handlers that expect it there)
        # If the invoice line already carries a GT (injected by caller), use that and note any SOF mismatch.
        # _parse_vessel_dimension handles European format: "23.403" → 23403 GT.
        # Use dim_type-aware priority — do NOT fall back across dimension types
        # (e.g. LOA meters should never be used as GT).
        if dim_type in ("GT",):
            _inv_raw = invoice_line.get("gt")
        elif dim_type in ("GRT",):
            _inv_raw = invoice_line.get("grt") or invoice_line.get("trb")
        else:  # LOA_meters, LOA
            _inv_raw = invoice_line.get("loa")
        invoice_dim_value = _parse_vessel_dimension(_inv_raw)
        if invoice_dim_value:
            # Use the invoice's own dimension value for calculation.
            # Also update dim_value so vessel_dimension_value in the output
            # reflects the dimension that was actually used (not the SOF value
            # which may be zero when the SOF has no vessel block).
            dim_value_for_line = invoice_dim_value
            if not dim_value:
                dim_value = invoice_dim_value
        else:
            # Fall back to SOF vessel dimension and inject it
            dim_value_for_line = dim_value
            if dim_value:
                dim_key = dim_type.lower().replace("_meters", "")
                invoice_line[dim_key] = dim_value

        # Validate: if dimension is still 0 and port requires it, flag for review
        if not dim_value_for_line and dim_type not in ("none", ""):
            human_review_flag = True
            human_review_reason = (
                f"{human_review_reason}; " if human_review_reason else ""
            ) + f"Missing vessel dimension ({dim_type}) — cannot calculate rate"
            logger.warning(f"Line {idx} ({service_type}): no {dim_type} dimension available")

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

        # Human Review Check — flags may already be set by dimension validation above
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

        # Zone Inference Flag — set if zone was absent OR fuzzy-matched from free text
        zone_inferred = (
            (invoice_line.get("zone") is None and profile.get("zone_determines_rate", False))
            or invoice_line.pop("_zone_inferred", False)
        )

        try:
            # 1. Handler
            # --- Separately-billed overtime line (e.g. Las Palmas "Servicios especiales") ---
            if service_type.lower() == "overtime":
                ot_surcharge_pct = float(profile.get("overtime_surcharge_pct") or 0.0)
                ot_rate = float(profile.get("overtime_hourly_rate") or 0.0)

                if ot_surcharge_pct:
                    # Percentage-of-base model (Tenerife/La Palma: 10% Extra Time surcharge,
                    # billed per tug as separate lines). Use the most recent non-OT line's
                    # expected amount as the base.
                    last_service_expected = next(
                        (r.expected_amount for r in reversed(line_item_results)
                         if r.service_description.lower() != "overtime"),
                        None
                    )
                    if last_service_expected and num_ot_lines:
                        expected_ot = round(
                            last_service_expected * ot_surcharge_pct / 100 / num_ot_lines, 2
                        )
                        tariff_rule_cited = (
                            f"{port} tariff — Extra Time surcharge: {ot_surcharge_pct:.0f}% "
                            f"of base EUR {last_service_expected:,.2f} / {num_ot_lines} tug(s) "
                            f"= EUR {expected_ot:,.2f}"
                        )
                    else:
                        expected_ot = round(invoiced_amount, 2)
                        tariff_rule_cited = (
                            f"{port} tariff — Extra Time surcharge {ot_surcharge_pct:.0f}% "
                            f"(no preceding service line found; accepted as invoiced)"
                        )
                elif ot_rate:
                    expected_ot = round(ot_rate, 2)
                    tariff_rule_cited = (
                        f"{port} tariff — Overtime per tug: "
                        f"EUR {ot_rate:,.2f}/hr (1 tug × 1 hr or fraction)"
                    )
                else:
                    raise ValueError(
                        f"Port '{port}' has an Overtime line but no "
                        f"'overtime_hourly_rate' or 'overtime_surcharge_pct' in its calculation profile."
                    )

                handler_result = {"base_rate": expected_ot, "total_rate": expected_ot}
                # OT lines don't have a dedicated SOF event — mark as found to avoid
                # confidence penalty; the related maneuver's SOF event is implicit
                sof_event_found = True
                sof_event_cited = "Overtime charge for associated berth/unberth maneuver"
            else:
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

            if handler_result.get("_duration_defaulted"):
                human_review_flag = True
                dur_note = (
                    "Duration missing from SOF and invoice — "
                    "defaulted to 1.0 hr; verify actual service duration"
                )
                human_review_reason = (
                    f"{human_review_reason}; {dur_note}" if human_review_reason else dur_note
                )

            # 2. Surcharges (skip for separately-billed overtime lines)
            surcharge_list = (
                [] if service_type.lower() == "overtime"
                else _build_surcharge_list(invoice_line, profile, port)
            )
            surcharge_report = None
            if surcharge_list:
                base_for_surcharges = float(handler_result.get("total_rate") or 0.0)
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

            if (overtime_handler_key
                    and profile.get("calculation_pattern") not in skip_overtime_patterns
                    and not separate_ot_lines
                    and service_type.lower() != "overtime"):
                actual_duration, _ot_dur_defaulted = _get_duration_hrs(sof_event, invoice_line)
                if _ot_dur_defaulted:
                    logger.warning("Overtime check: duration missing from SOF and invoice — defaulting to 1.0 hr")
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
                    base_for_overtime = float(handler_result.get("total_rate") or 0.0)
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
            # Skip for separately-billed OT lines: rate is fixed, not bracket-based
            known_surcharge_types = [s["type"] for s in surcharge_list]
            if service_type.lower() == "overtime":
                candidate_explanations = []
            else:
                candidate_explanations = _infer_plausible_conditions(
                    profile=profile,
                    tariff_data=tariff_data,
                    dim_value=dim_value_for_line,
                    tug_count=tug_count,
                    invoiced_amount=invoiced_amount,
                    known_surcharge_types=known_surcharge_types
                )

            # 4b. Contract discount — apply to handler total_rate if configured
            contract_discount_pct = _resolve_contract_discount_pct(profile, service_date)
            calculation_pattern = profile.get("calculation_pattern", "")
            if calculation_pattern == "concession_check_only":
                # No base tariff available — can only note the concession on record
                human_review_flag = True
                _cd_vendor = profile.get('contract_discount', {}).get('vendor', 'contract')
                discount_note = (
                    f"Concession-only port — base tariff not available. "
                    f"{_cd_vendor} concession: {contract_discount_pct:.1f}% discount on record. "
                    f"Manual verification required."
                    if contract_discount_pct else
                    "Concession-only port — base tariff not available. No discount on record."
                )
                human_review_reason = (
                    f"{human_review_reason}; {discount_note}" if human_review_reason else discount_note
                )
                tariff_rule_cited = f"{port} — {discount_note}"
            elif contract_discount_pct > 0 and handler_result.get("total_rate", 0.0) > 0:
                gross_rate = handler_result["total_rate"]
                discounted_rate = round(gross_rate * (1 - contract_discount_pct / 100), 2)
                handler_result = dict(handler_result)
                handler_result["total_rate"] = discounted_rate
                handler_result["gross_rate_pre_discount"] = gross_rate
                handler_result["contract_discount_pct"] = contract_discount_pct
                tariff_rule_cited = (
                    f"{tariff_rule_cited} | Contract discount {contract_discount_pct:.1f}% applied "
                    f"(gross {gross_rate:,.2f} -> net {discounted_rate:,.2f})"
                )

            # 5. Build Line Item
            # Compute expected amount to determine exact_tariff_match
            _hr = handler_result
            _expected_base = float(_hr.get('total_rate') or 0.0)
            _expected_surcharge = surcharge_report.total_surcharge_amount if surcharge_report else 0.0
            _expected_overtime = overtime_result.overtime_charge if overtime_result else 0.0
            _expected_total = _expected_base + _expected_surcharge + _expected_overtime
            if _expected_total > 0:
                _var_pct = abs((invoiced_amount - _expected_total) / _expected_total * 100)
            else:
                _var_pct = 100.0 if invoiced_amount > 0 else 0.0
            _exact_tariff_match = _var_pct <= match_tolerance_pct

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
                exact_tariff_match=_exact_tariff_match,
                has_open_doubts=has_open_doubts,
                tug_spec_from_invoice=tug_spec_from_invoice,
                zone_inferred=zone_inferred,
                candidate_explanations=candidate_explanations,
                match_tolerance_pct=match_tolerance_pct
            )

        except ZoneUnresolvableError as e:
            logger.warning(f"Port '{port}' line {idx} ('{service_type}'): zone unresolvable. {e}")
            line_result = build_line_item(
                line_number=idx,
                service_description=service_type,
                invoiced_amount=invoiced_amount,
                currency=currency,
                handler_result={"base_rate": 0.0, "total_rate": 0.0},
                sof_event_cited=sof_event_cited,
                tariff_rule_cited=f"ZONE_UNRESOLVED: {str(e)}",
                handler_used="zone_unresolved",
                sof_event_found=sof_event_found,
                exact_tariff_match=False,
                has_open_doubts=True,
                human_review_flag=True,
                human_review_reason=str(e),
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

    # 5. Compute invoice net amounts from adjustment lines
    _DISCOUNT_KW = ("discount", "rebate", "korting", "remise", "descuento")
    _FUEL_KW = ("bunker", "baf", "fuel", "brandstof")
    _discount_total = 0.0
    _fuel_surcharge_total = 0.0
    for _adj in (adjustment_lines or []):
        _desc = (_adj.get("description") or "").lower()
        _amt = abs(float(_adj.get("amount") or 0))
        if any(w in _desc for w in _DISCOUNT_KW):
            _discount_total += _amt
        elif any(w in _desc for w in _FUEL_KW):
            _fuel_surcharge_total += _amt
    _invoice_amount_gross = round(sum(li.invoiced_amount for li in line_item_results), 2)
    _invoice_amount_net = round(_invoice_amount_gross - _discount_total, 2)

    # 6. Assemble Full Result
    result = build_result(
        invoice_reference=invoice_reference,
        vendor=vendor,
        port=port,
        vessel_name=vessel_name,
        vessel_dimension_type=dim_type,
        vessel_dimension_value=dim_value,
        service_date=service_date,
        line_items=line_item_results,
        currency=currency,
        invoice_amount_gross=_invoice_amount_gross,
        fuel_surcharge_total=round(_fuel_surcharge_total, 2),
        invoice_amount_net=_invoice_amount_net,
    )

    return to_dict(result)


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
    match_tolerance_pct: float = 1.0,
    adjustment_lines: List[dict] = None,
) -> dict:
    """
    Public entry point.

    Detects whether invoice_lines span multiple service dates.
    - Single date (or all lines undated): delegates directly to _route_single_date()
      with no behaviour change.
    - Multiple dates: groups lines by date, calls _route_single_date() per group
      (each with its own service_date so SOF matching stays accurate), then merges.
    - Undated lines: fall back to the caller-supplied service_date, mirroring the
      original single-date behaviour and preventing silent data loss.
    """
    dated   = [l for l in invoice_lines if l.get("date")]
    undated = [l for l in invoice_lines if not l.get("date")]
    dates   = sorted(set(l["date"] for l in dated))

    # Fast path — single date and no undated lines: identical to old behaviour
    if len(dates) <= 1 and not undated:
        return _route_single_date(
            port=port, sof_data=sof_data, invoice_lines=invoice_lines,
            tariff_data=tariff_data, calculation_profiles=calculation_profiles,
            invoice_reference=invoice_reference, vendor=vendor,
            vessel_name=vessel_name, service_date=service_date,
            match_tolerance_pct=match_tolerance_pct, adjustment_lines=adjustment_lines,
        )

    # Multi-date path
    results = []
    for d in dates:
        group = [l for l in dated if l["date"] == d]
        results.append(_route_single_date(
            port=port, sof_data=sof_data, invoice_lines=group,
            tariff_data=tariff_data, calculation_profiles=calculation_profiles,
            invoice_reference=invoice_reference, vendor=vendor,
            vessel_name=vessel_name, service_date=d,
            match_tolerance_pct=match_tolerance_pct, adjustment_lines=adjustment_lines,
        ))

    if undated:
        results.append(_route_single_date(
            port=port, sof_data=sof_data, invoice_lines=undated,
            tariff_data=tariff_data, calculation_profiles=calculation_profiles,
            invoice_reference=invoice_reference, vendor=vendor,
            vessel_name=vessel_name, service_date=service_date,
            match_tolerance_pct=match_tolerance_pct, adjustment_lines=adjustment_lines,
        ))

    return _merge(results)
