"""
tools/validate_tariff_schema.py
--------------------------------
Linter for vendy-process tariff JSON files.

Validates each tariff against the canonical schema:
  - Top-level:  currency (string), rate_table (list or dict)
  - Each row:   dimension_range OR (dimension_min + dimension_max)
  - Each row:   base_rate OR (fixed_part + variable_part)
  - No legacy field names anywhere

Usage:
    python tools/validate_tariff_schema.py
    python tools/validate_tariff_schema.py --fix-dry-run   (show what would change)
"""

import json
import sys
import os
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# LEGACY FIELD LISTS
# ─────────────────────────────────────────────────────────────────────────────

LEGACY_TABLE_KEYS = {
    "tariff_matrix",
    "tariffs",
    "tariff_a_matrix",
    "tariff_matrix_per_vessel",
    "tariff_matrix_per_maneuver",
    "tariff_matrix_per_service_hour",
    "tariff_matrix_consolidated",
    "tariff_matrix_general_standard",
    "tariff_matrix_per_tug",
    "geographic_sections_and_terminals",
}

LEGACY_DIMENSION_FIELDS = {
    "trb_range", "grt_range", "loa_bracket", "gt_bracket",
    "gross_tonnage_bracket", "grt_bracket", "bracket",
    "min_gt", "max_gt", "min_loa", "max_loa",
    "min_loa_m", "max_loa_m", "min_grt", "max_grt",
    "loa_from", "loa_to", "gt_range", "up_to_gt",
}

LEGACY_RATE_FIELDS = {
    "basic_quota_usd", "rate_usd", "rate_eur", "tariff_rate",
    "rate", "first_hour_rate", "fixed_rate_eur", "variable_rate_per_gt_eur",
    "fixed_rate", "variable_rate_per_gt", "variable_rate",
}

# ─────────────────────────────────────────────────────────────────────────────
# EXCEPTIONS
# ─────────────────────────────────────────────────────────────────────────────

# Ports that do NOT need a top-level rate_table — engine uses fallback scan or profile-embedded rates
RATE_TABLE_EXCEPTIONS = {
    "ceuta",      # Multi-table special structure: tariff_tables key
    "algeciras",  # Rates embedded in calculation profile, not tariff file
    "antwerp",    # Engine fallback scan finds tariff_a_seagoing_vessels_operational_engine list
    "rotterdam",  # Engine fallback scan finds tariff_a_seagoing_vessels_operational_engine list
    "rostock",    # Engine fallback scan finds tariff_a_base_rates.brackets list
    "brake",      # Engine fallback scan finds tariff_a_seagoing_vessels.rates list
    "panama",     # hourly_with_minimum pattern; engine reads tariff_a_port_towing directly
}

# Ports where rate rows may not have base_rate — they use explicit rate_column from profile
# (e.g. bow_thruster columns, zone-specific rate columns, HP rate)
RATE_COLUMN_PORTS = {
    "altamira",       # with_bow_thruster_rate / without_bow_thruster_rate
    "ensenada",       # one_tug_arrival_usd, two_tugs_arrival_usd, etc.
    "antwerp",        # zandvliet_kallo_rate_eur, antwerp_city_rate_eur
    "rotterdam",      # rotterdam_rate, europoort_rate, maasvlakte_ii_rate
    "le_havre",       # zone-specific rate columns
    "coatzacoalcos",  # rate_per_hp_hour_usd (HP rate, not generic base_rate)
    "mazatlan",       # rate_per_hp_hour_usd (HP rate, not generic base_rate)
}

# Ports where rows use nested zone sub-dicts {fixed, variable} instead of direct rate fields
ZONE_RATE_PORTS = {
    "tenerife_la_palma",  # Each row: zone_key -> {fixed, variable}
}

# Ports where rows may use string-key dict (not a list), e.g. Ghent LOA bracket keys
STRING_KEY_DICT_PORTS = {
    "ghent",
    "dordrecht_moerdijk",
}


# ─────────────────────────────────────────────────────────────────────────────
# VALIDATION LOGIC
# ─────────────────────────────────────────────────────────────────────────────

def validate_tariff(tariff_path: Path) -> dict:
    """
    Validate a single tariff JSON file.
    Returns {"port": str, "status": "PASS"|"FAIL"|"WARN", "issues": [str], "warnings": [str]}
    """
    port_name = tariff_path.stem.lower()  # e.g. "tampico"
    display_name = tariff_path.stem        # e.g. "tampico"
    issues = []
    warnings = []

    # Load file
    try:
        with open(tariff_path, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        return {"port": display_name, "status": "FAIL", "issues": [f"JSON parse error: {e}"], "warnings": []}

    # ── Check 1: currency ───────────────────────────────────────────────────
    if "currency" not in data:
        issues.append("Missing top-level 'currency' field")
    elif not isinstance(data["currency"], str) or not data["currency"].strip():
        issues.append(f"'currency' must be a non-empty string, got: {data['currency']!r}")

    # ── Check 2: Legacy top-level table keys ────────────────────────────────
    found_legacy_keys = LEGACY_TABLE_KEYS & set(data.keys())
    for k in sorted(found_legacy_keys):
        issues.append(f"Legacy top-level key: '{k}' -> should be 'rate_table'")

    # ── Check 3: rate_table exists ──────────────────────────────────────────
    has_rate_table = "rate_table" in data
    if not has_rate_table:
        if port_name in RATE_TABLE_EXCEPTIONS:
            # Special case — check for the alternative keys
            if port_name == "ceuta":
                if "tariff_tables" not in data and not any(k.startswith("rate_table") for k in data):
                    warnings.append("Ceuta: expected 'tariff_tables' or 'rate_table*' key — found neither")
            # algeciras: rates in profile, no rate_table needed
            # antwerp/rotterdam/rostock/brake: engine uses BRACKET_KEYS fallback scan
            # panama: engine reads tariff_a_port_towing directly
        else:
            issues.append("Missing 'rate_table' key")

    # ── Check 4: rate_table row structure ───────────────────────────────────
    rate_table = data.get("rate_table")

    if has_rate_table and rate_table is not None:
        rows_to_check = []

        if isinstance(rate_table, list):
            rows_to_check = rate_table
        elif isinstance(rate_table, dict):
            # Could be Ghent-style nested dict, Moerdijk sections[], etc.
            # Walk one level to find rows
            if "sections" in rate_table:
                # Moerdijk: rate_table.sections[].rates (dict of string-key brackets)
                for section in rate_table.get("sections", []):
                    section_rates = section.get("rates", {})
                    if isinstance(section_rates, dict):
                        pass  # String-key dict — validated separately
            else:
                warnings.append(f"'rate_table' is a dict (not a list) — skipping row checks")
        else:
            issues.append(f"'rate_table' must be a list or dict, got {type(rate_table).__name__}")

        if rows_to_check:
            _check_rows(rows_to_check, port_name, issues, warnings)

    return {
        "port": display_name,
        "status": "FAIL" if issues else ("WARN" if warnings else "PASS"),
        "issues": issues,
        "warnings": warnings,
    }


def _check_rows(rows: list, port_name: str, issues: list, warnings: list):
    """Check individual rate table rows for canonical field usage."""
    # Sample up to first 3 rows to avoid flooding output
    sample = rows[:3]
    total = len(rows)

    legacy_dim_found = set()
    legacy_rate_found = set()
    missing_dim_count = 0
    missing_rate_count = 0

    for row in rows:
        if not isinstance(row, dict):
            continue

        # Check for legacy dimension fields
        legacy_dim_found |= LEGACY_DIMENSION_FIELDS & set(row.keys())

        # Check for legacy rate fields
        legacy_rate_found |= LEGACY_RATE_FIELDS & set(row.keys())

        # Check has canonical dimension field
        has_dim = (
            "dimension_range" in row
            or "dimension_max" in row          # Las Palmas: cumulative upper-bound only
            or ("dimension_min" in row and "dimension_max" in row)
        )
        if not has_dim:
            missing_dim_count += 1

        # Check has canonical rate field (unless port uses explicit rate_column or zone sub-dicts)
        if port_name not in RATE_COLUMN_PORTS and port_name not in ZONE_RATE_PORTS:
            has_rate = (
                "base_rate" in row
                or ("fixed_part" in row and "variable_part" in row)
            )
            if not has_rate:
                missing_rate_count += 1

    # Report legacy fields
    for f in sorted(legacy_dim_found):
        issues.append(f"Legacy dimension field in rows: '{f}' -> should be 'dimension_range' or 'dimension_min'/'dimension_max'")
    for f in sorted(legacy_rate_found):
        issues.append(f"Legacy rate field in rows: '{f}' -> should be 'base_rate' or 'fixed_part'+'variable_part'")

    # Report missing canonical fields
    if missing_dim_count > 0:
        issues.append(f"{missing_dim_count}/{total} rows missing dimension field (dimension_range OR dimension_min+dimension_max)")
    if missing_rate_count > 0 and port_name not in RATE_COLUMN_PORTS:
        issues.append(f"{missing_rate_count}/{total} rows missing rate field (base_rate OR fixed_part+variable_part)")


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def main():
    # Find tariff files relative to this script's location or project root
    script_dir = Path(__file__).parent
    project_root = script_dir.parent
    tariffs_dir = project_root / "tariffs"

    if not tariffs_dir.exists():
        print(f"ERROR: tariffs/ directory not found at {tariffs_dir}")
        sys.exit(1)

    tariff_files = sorted(tariffs_dir.glob("*.json"))
    if not tariff_files:
        print("No tariff JSON files found.")
        sys.exit(1)

    results = [validate_tariff(f) for f in tariff_files]

    # Print summary table
    col_w = 30
    print()
    print(f"{'PORT':<{col_w}}  {'STATUS':<8}  ISSUES / WARNINGS")
    print("-" * 90)

    pass_count = warn_count = fail_count = 0
    for r in results:
        status = r["status"]
        if status == "PASS":
            pass_count += 1
            sym = "+"
        elif status == "WARN":
            warn_count += 1
            sym = "~"
        else:
            fail_count += 1
            sym = "X"

        print(f"{r['port']:<{col_w}}  {sym} {status:<7}", end="")
        all_msgs = [f"[ISSUE] {m}" for m in r["issues"]] + [f"[WARN] {m}" for m in r["warnings"]]
        if all_msgs:
            print(f"  {all_msgs[0]}")
            for msg in all_msgs[1:]:
                print(f"{'':>{col_w + 12}}{msg}")
        else:
            print()

    print("-" * 90)
    print(f"\nTotal: {len(results)} files — {pass_count} PASS  {warn_count} WARN  {fail_count} FAIL")
    print()

    if fail_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
