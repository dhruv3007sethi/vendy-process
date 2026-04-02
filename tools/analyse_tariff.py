#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tools/analyse_tariff.py
-----------------------
Analyses a port tariff JSON file and reports:

  1. Whether the rate table is findable with existing engine keys
  2. Which calculation pattern the structure matches (if any)
  3. Which field names are known vs unknown
  4. Structural anomalies that would require a new calculation handler
  5. A clear recommendation: ready to test / minor fixes needed / new handler required

Usage:
    python tools/analyse_tariff.py tariffs/new_port.json
    python tools/analyse_tariff.py tariffs/new_port.json --verbose

Run against all existing tariff files to confirm they all pass:
    python tools/analyse_tariff.py --all
"""

import json
import sys
import os
import re
import argparse
from pathlib import Path

# Force UTF-8 output on Windows so unicode symbols render correctly
if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')


# ---------------------------------------------------------------------------
# KNOWN VOCABULARIES
# ---------------------------------------------------------------------------

# All top-level rate table keys the engine can currently find
KNOWN_RATE_TABLE_KEYS = {
    # canonical (target for standardisation)
    'rate_table',
    # Spanish ports
    'tariff_matrix',
    'tariff_matrix_general_standard',
    'tariff_matrix_consolidated',
    'geographic_sections_and_terminals',
    'tariff_calculation_logic',        # Algeciras
    'general_maximum_tariff_zone_i',   # Castellon
    # Mexican ports
    'tariff_matrix_per_vessel',
    'tariff_matrix_per_maneuver',
    'tariff_matrix_per_service_hour',
    'tariff_matrix_per_hp_hour',       # Mazatlan
    'tariff_matrix_per_tug',           # Altamira, Coatzacoalcos, Santo Domingo
    # European ports
    'tariffs',
    'tariff_a_matrix',
    'tariff_a_seagoing_vessels_operational_engine',  # Antwerp, Rotterdam
    'tariff_a_seagoing_vessels',                     # Brake
    'tariff_a_base_rates',                           # Rostock
    'tariff_a_port_towing',                          # Panama
    # Ceuta (multiple tariff tables — any of these counts)
    'tariff_tables',
    'tariff_t0',
    'tariff_t_plus_1',
    'tariff_t_minus_1',
    'tariff_t_plus_2',
    'tariff_t_minus_2',
}

# Bracket / dimension range field names the engine recognises
KNOWN_BRACKET_FIELDS = {
    # canonical
    'dimension_range', 'dimension_min', 'dimension_max',
    # text-range styles
    'trb_range', 'grt_range', 'grt_bracket', 'loa_bracket', 'gt_bracket',
    'gross_tonnage_bracket', 'bracket', 'accumulated_gt_range',
    # numeric min/max styles — both orderings (min_gt and gt_min)
    'min_gt', 'max_gt', 'gt_min', 'gt_max',
    'min_loa', 'max_loa', 'min_loa_m', 'max_loa_m',
    'min_grt', 'max_grt',
    'loa_from', 'loa_to', 'up_to_gt', 'up_to_loa',
    'gt_range', 'loa_range',
}

# Rate value field names the engine recognises
KNOWN_RATE_FIELDS = {
    # canonical
    'base_rate',
    # non-canonical but in engine fallback lists
    'rate', 'rate_eur', 'rate_usd', 'tariff_rate',
    'first_hour_rate', 'basic_quota_usd',
    # fixed_plus_variable
    'fixed_part', 'variable_part', 'fixed_component', 'variable_component',
    'fixed_amount_if', 'variable_amount_iv',   # Valencia
    'vp', 'fp',                                # Huelva
    'fixed_rate_eur', 'variable_rate_per_gt_eur',  # Algeciras
    # hp_hourly
    'hp_rate', 'rate_per_hp', 'hp_rate_per_hour', 'rate_per_hp_hour_usd',
    # hourly_with_minimum
    'hourly_rate', 'minimum_charge', 'minimum_hours',
    'base_rate_usd', 'minimum_charge_usd',             # Panama
    # per_service_tug_count_specific (Ensenada: tug count × direction columns)
    'rate_1_tug', 'rate_2_tugs', 'rate_3_tugs', 'rate_4_tugs',
    'one_tug_arrival_usd', 'two_tugs_arrival_usd',
    'one_tug_departure_usd', 'two_tugs_departure_usd',
    # fixed_plus_variable variants
    'fixed_part_eur', 'variable_part_eur_per_gt',      # Castellon
    # zone-specific rate columns (bracket_lookup with multiple zone columns)
    'zandvliet_kallo_rate_eur', 'berendrecht_zandvliet_kallo_rate_eur',
    'antwerp_city_rate_eur', 'hansen_rate_eur',        # Antwerp
    'europoort_rate', 'maasvlakte_ii_rate', 'rotterdam_rate',  # Rotterdam
    'with_bow_thruster_rate', 'without_bow_thruster_rate',
}

# Top-level keys that are metadata — not rate tables
KNOWN_METADATA_KEYS = {
    'port_metadata', 'geographic_scope_and_terminals', 'geographic_scope',
    'billing_logic_definitions', 'surcharges_discounts_and_multipliers',
    'special_maneuver_hourly_rates', 'special_services', 'waiting_time',
    'displacement_fee', 'tariff_notes', 'overtime_rules', 'service_definitions',
    'notes', 'currency', 'port', 'description', 'effective_date',
    'port_name', 'country', 'provider',
}

# Row-level metadata fields (not bracket or rate — just annotation)
KNOWN_ROW_ANNOTATION_FIELDS = {
    'description', 'notes', 'service_type', 'terminal', 'zone',
    'currency', 'tug_type', 'vessel_type', 'id', 'route',
    'max_duration_hrs', 'max_breadth', 'max_draft', 'definition',
    'rates', 'brackets', 'terminals',
}

# Canonical field names (what we want everything to become)
CANONICAL_FIELDS = {
    'base_rate', 'dimension_range', 'dimension_min', 'dimension_max',
    'fixed_part', 'variable_part', 'rate_table', 'currency',
}

# Keywords that signal anomalies requiring new handlers
TIME_KEYWORDS      = {'night', 'day_rate', 'weekend', 'holiday', 'hour_from',
                      'hour_to', 'time_band', 'daylight', 'after_hours'}
VOLUME_KEYWORDS    = {'monthly', 'volume_band', 'move_count', 'annual_moves',
                      'contract_moves', 'discount_band', 'cumulative', 'tier_'}
UNKNOWN_DIM_KEYWORDS = {'dwt', 'displacement', 'nrt', 'net_tonnage', 'deadweight'}
PROGRESSIVE_KEYWORDS = {'progressive_rate', 'incremental_rate', 'accumulated_rate', 'cumulative_rate'}
MULTI_CURRENCY_RATE  = {'rate_eur', 'rate_usd', 'basic_quota_usd', 'rate_mxn'}


# ---------------------------------------------------------------------------
# RATE TABLE FINDER
# ---------------------------------------------------------------------------

def _find_rate_table(tariff_data: dict):
    """Try all known top-level keys. Returns (key_found, raw_value) or (None, None)."""
    for key in KNOWN_RATE_TABLE_KEYS:
        if key in tariff_data:
            return key, tariff_data[key]
    return None, None


# ---------------------------------------------------------------------------
# ROW COLLECTOR
# ---------------------------------------------------------------------------

def _collect_rows(rate_table_raw) -> list:
    """
    Flatten any rate table structure to a list of row dicts for field analysis.
    Handles: list-of-dicts, sections array (Moerdijk), zone-keyed dict.
    """
    if isinstance(rate_table_raw, list):
        rows = []
        for item in rate_table_raw:
            if not isinstance(item, dict):
                continue
            if 'id' in item and 'rates' in item:
                # Moerdijk sections: annotate with section id and flatten rates
                rates = item['rates']
                if isinstance(rates, dict):
                    rows.append({**rates, '_section_id': item['id']})
                elif isinstance(rates, list):
                    for r in rates:
                        if isinstance(r, dict):
                            rows.append({**r, '_section_id': item['id']})
            else:
                rows.append(item)
        return rows

    if isinstance(rate_table_raw, dict):
        vals = list(rate_table_raw.values())
        if not vals:
            return []
        # Pure string-key rate dict (Moerdijk rates sub-dict, Le Havre format)
        if all(isinstance(v, (int, float)) for v in vals):
            return []  # Handled separately as string-key brackets

        # Wrapper dict with a 'brackets' or 'rates' sub-list at top level
        # e.g. {"description": "...", "brackets": [...]}  — Rostock
        # e.g. {"formula": "...", "rates": {...}}          — Algeciras
        for sub_key in ('brackets', 'rates', 'tariffs'):
            if sub_key in rate_table_raw:
                sub = rate_table_raw[sub_key]
                if isinstance(sub, list):
                    return [item for item in sub if isinstance(item, dict)]
                if isinstance(sub, dict):
                    # Could be zone-keyed dict of lists (Algeciras rates)
                    sub_rows = _collect_rows(sub)
                    if sub_rows:
                        return sub_rows
                    # Or a flat dict of zone-name → {fixed, variable} (Algeciras flat)
                    return [sub]

        # Zone-keyed: {"zone_a": [...], "zone_b": [...]}
        if any(isinstance(v, list) for v in vals):
            rows = []
            for v in vals:
                if isinstance(v, list):
                    for item in v:
                        if isinstance(item, dict):
                            # If the item itself has a 'rates' sub-list, go deeper
                            if 'rates' in item and isinstance(item['rates'], list):
                                rows.extend(r for r in item['rates'] if isinstance(r, dict))
                            else:
                                rows.append(item)
            return rows

        # Dict of dicts — each value is a zone or sub-section
        if all(isinstance(v, dict) for v in vals):
            # Check if each value has a 'rates' or 'tariffs' sub-list (Valencia, Huelva, Cadiz Bay)
            deep_rows = []
            for v in vals:
                sub_list = None
                for sub_key in ('rates', 'tariffs', 'brackets'):
                    if sub_key in v and isinstance(v[sub_key], list):
                        sub_list = v[sub_key]
                        break
                if sub_list is not None:
                    deep_rows.extend(r for r in sub_list if isinstance(r, dict))
                else:
                    # Ghent pattern: dict has list-of-dict values keyed by zone name
                    zone_lists = [
                        v2 for v2 in v.values()
                        if isinstance(v2, list) and v2 and isinstance(v2[0], dict)
                    ]
                    if zone_lists:
                        for zone_list in zone_lists:
                            deep_rows.extend(r for r in zone_list if isinstance(r, dict))
                    else:
                        deep_rows.append(v)
            return deep_rows

    return []


# ---------------------------------------------------------------------------
# PATTERN DETECTOR
# ---------------------------------------------------------------------------

def _detect_pattern(rows: list, rate_table_raw) -> tuple:
    """
    Infer the most likely calculation pattern.
    Returns (pattern_name, confidence, explanation).
    """
    # all_keys = union across every row (some fields only appear in select rows)
    all_keys = set()
    for r in rows:
        if isinstance(r, dict):
            all_keys.update(r.keys())
    # String-key dict → bracket_lookup (Format 1)
    if isinstance(rate_table_raw, dict):
        vals = list(rate_table_raw.values())
        if vals and all(isinstance(v, (int, float)) for v in vals):
            return (
                'bracket_lookup',
                'HIGH',
                'Rate table is a flat dict with string bracket keys (e.g. "up_to_138m": 3520)',
            )
        # Sections array (Moerdijk)
        if 'sections' in rate_table_raw:
            return (
                'bracket_lookup',
                'HIGH',
                'Sections array — each section has an id + rates dict (Moerdijk pattern)',
            )

    if not rows:
        return ('UNKNOWN', 'LOW', 'Rate table is empty or structure not recognised')

    first = rows[0]
    if not isinstance(first, dict):
        return ('UNKNOWN', 'LOW', f'First row is {type(first).__name__}, expected dict')

    keys = set(first.keys()) - {'_section_id'}

    # bracket_lookup_with_formula — Ceuta pattern
    # Rows have: table_id + bracket field + fixed_rates (dict) + formula for large vessels
    if 'table_id' in all_keys and any(k in all_keys for k in KNOWN_BRACKET_FIELDS):
        return (
            'bracket_lookup_with_formula',
            'HIGH',
            'Ceuta pattern: table_id + bracket field + per-table fixed_rates + formula above threshold',
        )

    # fixed_plus_variable — multiple naming conventions across ports
    FIXED_ALIASES = ('fixed_part', 'fixed_component', 'fixed_amount_if',
                     'fixed_rate_eur', 'fixed_part_eur', 'fp')
    VAR_ALIASES   = ('variable_part', 'variable_component', 'variable_amount_iv',
                     'variable_rate_per_gt_eur', 'variable_part_eur_per_gt', 'vp')
    has_fixed = any(k in keys for k in FIXED_ALIASES)
    has_var   = any(k in keys for k in VAR_ALIASES)
    if has_fixed and has_var:
        return (
            'fixed_plus_variable',
            'HIGH',
            f'Row has fixed component ({[k for k in keys if k in FIXED_ALIASES]}) '
            f'+ variable component ({[k for k in keys if k in VAR_ALIASES]})',
        )

    # fixed_plus_variable — zone-as-columns (Tenerife/La Palma pattern)
    # Rows have a bracket key + several zone-name columns whose values are
    # dicts with 'fixed'/'variable' sub-keys
    bracket_cols = KNOWN_BRACKET_FIELDS & keys
    if bracket_cols:
        zone_cols = [
            k for k in keys - bracket_cols - KNOWN_ROW_ANNOTATION_FIELDS
            if isinstance(first.get(k), dict)
            and any(sub in first[k] for sub in ('fixed', 'variable', 'fixed_part', 'variable_part'))
        ]
        if zone_cols:
            return (
                'fixed_plus_variable',
                'HIGH',
                f'Zone-as-columns pattern: bracket={sorted(bracket_cols)}, '
                f'zones with fixed/variable sub-keys={zone_cols[:3]}',
            )

    # hp_hourly
    hp_keys = {k for k in keys if any(x in k.lower() for x in ('hp_rate', 'rate_per_hp', 'hp_rating'))}
    if hp_keys:
        return ('hp_hourly', 'HIGH', f'Row has HP rate field(s): {sorted(hp_keys)}')

    # hourly_with_minimum
    if 'hourly_rate' in keys and any(k in keys for k in ('minimum_charge', 'minimum_hours')):
        return (
            'hourly_with_minimum',
            'HIGH',
            'Row has hourly_rate + minimum_charge/minimum_hours',
        )

    # per_service_tug_count_specific — Ensenada pattern:
    # Columns named one_tug_arrival_usd, two_tugs_arrival_usd, one_tug_departure_usd, etc.
    tug_direction_keys = {
        k for k in all_keys
        if re.match(r'(one|two)_tug[s]?_(arrival|departure)', k)
        or re.match(r'rate_\d+_tug', k)
    }
    if tug_direction_keys:
        return (
            'per_service_tug_count_specific',
            'HIGH',
            f'Row has tug-count × direction rate columns: {sorted(tug_direction_keys)[:4]}',
        )

    # bracket_lookup — text range or numeric min/max
    bracket_fields_present = KNOWN_BRACKET_FIELDS & keys
    rate_fields_present    = KNOWN_RATE_FIELDS & keys
    numeric_bracket = {k for k in keys if k.startswith(('min_', 'max_')) or k in ('loa_from', 'loa_to')}

    if bracket_fields_present and rate_fields_present:
        return (
            'bracket_lookup',
            'HIGH',
            f'Row has bracket field(s) {sorted(bracket_fields_present)} '
            f'+ rate field(s) {sorted(rate_fields_present)}',
        )
    if numeric_bracket and rate_fields_present:
        return (
            'bracket_lookup',
            'HIGH',
            f'Row has numeric min/max fields {sorted(numeric_bracket)} '
            f'+ rate field(s) {sorted(rate_fields_present)}',
        )

    # hourly_with_minimum — Panama-style (check all_keys, as min_charge may not be in first row)
    if any(k in all_keys for k in ('base_rate_usd', 'hourly_rate')) and \
       any(k in all_keys for k in ('minimum_charge_usd', 'minimum_charge', 'minimum_hours',
                                   'billing_unit', 'area')):
        return (
            'hourly_with_minimum',
            'HIGH',
            f'Row has hourly rate field + minimum charge field — hourly_with_minimum pattern',
        )

    # bracket_lookup — multiple zone-specific rate columns (Rotterdam, Antwerp city rate)
    zone_rate_cols = {k for k in keys if k.endswith('_rate') or k.endswith('_rate_eur')}
    if (bracket_fields_present or numeric_bracket) and zone_rate_cols:
        return (
            'bracket_lookup',
            'HIGH',
            f'Row has bracket field(s) + zone-specific rate columns {sorted(zone_rate_cols)[:3]}',
        )

    # Rate fields with no bracket — flat rate
    if rate_fields_present:
        return (
            'FLAT_RATE',
            'MEDIUM',
            f'Row has rate field(s) {sorted(rate_fields_present)} but no bracket dimension',
        )

    # Deep scan: look for string bracket keys nested inside a 'rates' dict within this row
    # Handles Ghent-style rows: {"route": "...", "rates": {"up_to_114.99": 1450, ...}}
    for k, v in first.items():
        if k == 'rates' and isinstance(v, dict) and v:
            sample_val = next(iter(v.values()))
            if isinstance(sample_val, (int, float)):
                return (
                    'bracket_lookup',
                    'HIGH',
                    f'Row contains a nested rates dict with string bracket keys — bracket_lookup pattern',
                )

    return ('UNKNOWN', 'LOW', f'No known pattern signature. Row keys: {sorted(keys)}')


# ---------------------------------------------------------------------------
# FIELD NAME AUDITOR
# ---------------------------------------------------------------------------

def _audit_fields(rows: list) -> tuple:
    """
    Check every field name in rows against known vocabularies.
    Returns (canonical, known_aliases, unknown) — all sets.
    """
    all_keys = set()
    for row in rows:
        if isinstance(row, dict):
            all_keys.update(k for k in row.keys() if not k.startswith('_'))

    canonical     = all_keys & CANONICAL_FIELDS
    known_aliases = (all_keys & (KNOWN_BRACKET_FIELDS | KNOWN_RATE_FIELDS | KNOWN_ROW_ANNOTATION_FIELDS)) - canonical
    unknown       = all_keys - canonical - known_aliases

    return canonical, known_aliases, unknown


# ---------------------------------------------------------------------------
# ANOMALY DETECTOR
# ---------------------------------------------------------------------------

def _detect_anomalies(rows: list) -> list:
    """
    Scan for structural patterns that no existing handler can process.
    Returns list of (severity, label, description).
    Severity: 'ERROR' = new handler required, 'WARNING' = needs investigation, 'INFO' = note.
    """
    anomalies = []
    if not rows:
        return anomalies

    all_keys = set()
    for row in rows:
        if isinstance(row, dict):
            all_keys.update(k.lower() for k in row.keys() if not k.startswith('_'))

    # Dual dimension
    gt_keys  = {k for k in all_keys if any(x in k for x in ('_gt', 'gt_', 'grt', 'trb')) and 'rate' not in k}
    loa_keys = {k for k in all_keys if 'loa' in k and 'rate' not in k}
    if gt_keys and loa_keys:
        anomalies.append((
            'WARNING',
            'DUAL DIMENSION',
            f'Both GT/GRT fields {sorted(gt_keys)} and LOA fields {sorted(loa_keys)} '
            'are present in the same rows. The engine reads one dimension per port — '
            'if both are required simultaneously a new handler is needed.',
        ))

    # Time-of-day / day-of-week pricing
    time_keys = {k for k in all_keys if any(t in k for t in TIME_KEYWORDS)}
    if time_keys:
        anomalies.append((
            'ERROR',
            'TIME-BASED PRICING',
            f'Time/day-sensitive rate fields found: {sorted(time_keys)}. '
            'No existing handler supports time-of-day rate switching. New handler required.',
        ))

    # Volume / contract discount tiers
    vol_keys = {k for k in all_keys if any(v in k for v in VOLUME_KEYWORDS)}
    if vol_keys:
        anomalies.append((
            'ERROR',
            'VOLUME / TIERED DISCOUNT PRICING',
            f'Volume or tier-discount fields found: {sorted(vol_keys)}. '
            'The engine processes each invoice independently — '
            'multi-move volume tracking is not supported. New handler required.',
        ))

    # Unknown dimension unit
    dim_keys = {k for k in all_keys if any(d in k for d in UNKNOWN_DIM_KEYWORDS)}
    if dim_keys:
        anomalies.append((
            'ERROR',
            'UNKNOWN DIMENSION UNIT',
            f'Unsupported dimension fields: {sorted(dim_keys)}. '
            'The engine supports GT, GRT, LOA, TRB, HP only. '
            'A new dimension requires engine extension.',
        ))

    # Progressive / cumulative rate structure
    prog_keys = {k for k in all_keys if any(p in k for p in PROGRESSIVE_KEYWORDS)}
    if prog_keys:
        anomalies.append((
            'ERROR',
            'PROGRESSIVE / CUMULATIVE RATES',
            f'Fields suggesting accumulative billing: {sorted(prog_keys)}. '
            'The engine uses a flat rate per bracket — '
            'progressive accumulation across bands requires a new handler.',
        ))

    # Multiple currency rate fields in same rows
    currency_rate_keys = {k for k in all_keys if k in {f.lower() for f in MULTI_CURRENCY_RATE}}
    if len(currency_rate_keys) > 1:
        anomalies.append((
            'WARNING',
            'MULTI-CURRENCY RATES IN SAME ROWS',
            f'Multiple currency rate fields found together: {sorted(currency_rate_keys)}. '
            'The engine uses a single currency per port — '
            'verify which rate field applies and whether FX conversion is needed.',
        ))

    return anomalies


# ---------------------------------------------------------------------------
# REPORT PRINTER
# ---------------------------------------------------------------------------

def _print_report(path: Path, tariff_data: dict, verbose: bool):
    print(f"\n{'='*62}")
    print(f"  TARIFF ANALYSIS: {path.name}")
    print(f"{'='*62}")

    # Currency
    currency_raw = (
        tariff_data.get('currency') or
        (tariff_data.get('port_metadata') or {}).get('currency', 'NOT FOUND')
    )
    currency = re.sub(r'\s*\(.*\)', '', str(currency_raw)).strip()
    at_root  = 'currency' in tariff_data

    print(f"\nCURRENCY")
    print(f"  Value    : {currency}")
    if at_root:
        print(f"  Location : top-level  [OK]")
    else:
        print(f"  Location : inside port_metadata only  "
              f"[engine may default to EUR — add top-level key]")

    # Rate table
    print(f"\nRATE TABLE")
    key_found, rate_table_raw = _find_rate_table(tariff_data)

    if key_found is None:
        unknown_keys = sorted(
            set(tariff_data.keys()) - KNOWN_METADATA_KEYS - KNOWN_RATE_TABLE_KEYS
        )
        print(f"  ✗ NOT FOUND — none of the known keys matched")
        print(f"  Top-level keys in file : {sorted(tariff_data.keys())}")
        if unknown_keys:
            print(f"  Unrecognised keys      : {unknown_keys}")
        print(f"\nRECOMMENDATION")
        print(f"  ✗ ENGINE CHANGE NEEDED")
        print(f"    Add the rate table key to _get_rates_data in core/port_router.py")
        print(f"    OR rename the key to 'rate_table' in the JSON file")
        print(f"{'='*62}\n")
        return False  # not clean

    canonical_key = key_found == 'rate_table'
    print(f"  Found at key : '{key_found}'  "
          f"{'[canonical]' if canonical_key else '[non-canonical — alias already in engine]'}")

    # Describe structure
    rows = []
    if isinstance(rate_table_raw, dict) and 'sections' in rate_table_raw:
        sections = rate_table_raw['sections']
        print(f"  Structure    : Sections array — {len(sections)} section(s)")
        rows = _collect_rows(rate_table_raw)
    elif isinstance(rate_table_raw, dict):
        vals = list(rate_table_raw.values())
        if vals and all(isinstance(v, (int, float)) for v in vals):
            print(f"  Structure    : String-key bracket dict — {len(rate_table_raw)} keys")
        else:
            rows = _collect_rows(rate_table_raw)
            print(f"  Structure    : Zone-keyed dict — {len(rate_table_raw)} zone(s), {len(rows)} total rows")
    elif isinstance(rate_table_raw, list):
        rows = _collect_rows(rate_table_raw)
        print(f"  Structure    : List — {len(rate_table_raw)} row(s)")
    else:
        print(f"  Structure    : Unrecognised ({type(rate_table_raw).__name__})")

    # Pattern
    print(f"\nPATTERN MATCH")
    pattern, confidence, explanation = _detect_pattern(rows, rate_table_raw)
    icon = {'HIGH': '✓', 'MEDIUM': '~', 'LOW': '✗'}.get(confidence, '?')
    handler = _handler_file(pattern)

    print(f"  Likely pattern   : {pattern}  {icon}  ({confidence} confidence)")
    print(f"  Basis            : {explanation}")
    if handler:
        print(f"  Existing handler : core/handlers/{handler}")
    else:
        print(f"  Existing handler : NONE — new handler required")

    # Field names
    print(f"\nFIELD NAMES")
    if rows:
        canonical, aliases, unknown = _audit_fields(rows)
        if canonical:
            print(f"  Canonical      : {sorted(canonical)}")
        if aliases:
            print(f"  Known aliases  : {sorted(aliases)}  [engine handles these]")
        if unknown:
            print(f"  ⚠ Unknown      : {sorted(unknown)}  [not in engine vocabulary]")
        else:
            print(f"  Unknown        : none")
        if verbose and (canonical or aliases):
            print(f"  (All row keys  : {sorted(canonical | aliases | unknown)})")
    else:
        # String-key dict — the keys themselves are the brackets
        if isinstance(rate_table_raw, dict):
            sample = list(rate_table_raw.keys())[:6]
            print(f"  Bracket keys   : {sample}{'...' if len(rate_table_raw) > 6 else ''}")
            print(f"  (String-key format — no row field name issues)")
        unknown = set()

    # Anomalies
    print(f"\nSTRUCTURAL ANOMALIES")
    anomalies = _detect_anomalies(rows)
    if not anomalies:
        print(f"  None detected")
    else:
        for severity, label, desc in anomalies:
            sev_icon = {'ERROR': '✗', 'WARNING': '⚠', 'INFO': 'ℹ'}.get(severity, ' ')
            print(f"  {sev_icon} [{severity}] {label}")
            # Wrap description at 70 chars
            words = desc.split()
            line, indent = '', '      '
            for word in words:
                if len(line) + len(word) + 1 > 70:
                    print(f"{indent}{line}")
                    line = word
                else:
                    line = (line + ' ' + word).strip()
            if line:
                print(f"{indent}{line}")

    # Recommendation
    print(f"\nRECOMMENDATION")
    error_anomalies   = [a for a in anomalies if a[0] == 'ERROR']
    warning_anomalies = [a for a in anomalies if a[0] == 'WARNING']

    if error_anomalies:
        print(f"  ✗ ENGINE CHANGE NEEDED — new calculation handler required")
        for _, label, _ in error_anomalies:
            print(f"    Reason: {label}")
        print(f"{'='*62}\n")
        return False

    if pattern in ('UNKNOWN', 'FLAT_RATE'):
        print(f"  ✗ ENGINE CHANGE NEEDED — pattern not recognised")
        print(f"    Action : Identify the billing formula from the port tariff document")
        print(f"             then write a new handler in core/handlers/")
        print(f"{'='*62}\n")
        return False

    actions = []
    if not canonical_key:
        actions.append(
            f"Add '{key_found}' to _get_rates_data lookup in core/port_router.py  "
            f"OR rename to 'rate_table' in the JSON"
        )
    if rows:
        _, _, unk = _audit_fields(rows)
        if unk:
            actions.append(
                f"Unknown field(s) {sorted(unk)} — add to rate field fallback list "
                f"in core/handlers/bracket_lookup.py  OR rename to 'base_rate' in JSON"
            )
    if not at_root:
        actions.append(
            "Add top-level \"currency\" key to JSON "
            "(engine reads tariff_data['currency'], not port_metadata)"
        )
    for _, label, _ in warning_anomalies:
        actions.append(f"Investigate: {label}")

    if not actions:
        print(f"  ✓ READY TO TEST — no engine changes needed")
        print(f"    Pattern '{pattern}' fully handled by existing engine")
    else:
        print(f"  ~ MINOR FIXES NEEDED before testing ({len(actions)} item(s))")
        for i, action in enumerate(actions, 1):
            print(f"    {i}. {action}")

    print(f"{'='*62}\n")
    return len(error_anomalies) == 0 and pattern not in ('UNKNOWN', 'FLAT_RATE')


def _handler_file(pattern: str) -> str:
    return {
        'bracket_lookup':                    'bracket_lookup.py',
        'fixed_plus_variable':               'fixed_plus_variable.py',
        'hp_hourly':                         'hp_hourly.py',
        'hourly_with_minimum':               'hourly_with_minimum.py',
        'per_service_tug_count_specific':    'per_service_tug_count.py',
        'bracket_lookup_with_formula':       'bracket_lookup_with_formula.py',
    }.get(pattern, '')


# ---------------------------------------------------------------------------
# BATCH MODE — run against all tariffs
# ---------------------------------------------------------------------------

def analyse_all(tariffs_dir: Path, verbose: bool):
    files = sorted(tariffs_dir.glob('*.json'))
    if not files:
        print(f"No JSON files found in {tariffs_dir}")
        return

    results = []
    for f in files:
        with open(f, encoding='utf-8') as fp:
            try:
                data = json.load(fp)
            except json.JSONDecodeError as e:
                print(f"\n✗ {f.name}: JSON parse error — {e}")
                results.append((f.name, False))
                continue
        ok = _print_report(f, data, verbose)
        results.append((f.name, ok))

    print(f"\n{'='*62}")
    print(f"  BATCH SUMMARY — {len(files)} files")
    print(f"{'='*62}")
    passed  = [(n, ok) for n, ok in results if ok]
    blocked = [(n, ok) for n, ok in results if not ok]
    for name, _ in passed:
        print(f"  ✓  {name}")
    for name, _ in blocked:
        print(f"  ✗  {name}")
    print(f"\n  PASS: {len(passed)}   FAIL/BLOCKED: {len(blocked)}")
    print(f"{'='*62}\n")


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Analyse a port tariff JSON file against vendy-process engine capabilities.'
    )
    parser.add_argument(
        'tariff_file',
        nargs='?',
        help='Path to tariff JSON file (omit with --all to scan tariffs/ directory)',
    )
    parser.add_argument(
        '--all', '-a',
        action='store_true',
        help='Analyse all JSON files in the tariffs/ directory',
    )
    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Show additional detail',
    )
    args = parser.parse_args()

    root = Path(__file__).parent.parent  # vendy-process/

    if args.all:
        analyse_all(root / 'tariffs', args.verbose)
    elif args.tariff_file:
        path = Path(args.tariff_file)
        if not path.is_absolute():
            path = root / path
        if not path.exists():
            print(f"ERROR: File not found: {path}")
            sys.exit(1)
        with open(path, encoding='utf-8') as f:
            try:
                data = json.load(f)
            except json.JSONDecodeError as e:
                print(f"ERROR: JSON parse error in {path.name}: {e}")
                sys.exit(1)
        _print_report(path, data, args.verbose)
    else:
        parser.print_help()
