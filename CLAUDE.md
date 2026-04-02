# vendy-process — Project Guide for Claude Sessions

## What This Project Does

An automated **tugboat invoice verification engine** for a ship management company (Scorpio). When a port agent submits a towage invoice, this engine:

1. Accepts the invoice line items, a Statement of Facts (SOF), and the port's tariff JSON
2. Calculates the expected charge from the tariff
3. Compares expected vs invoiced amounts per line
4. Returns a structured verdict: AUTO_APPROVED / MISMATCH / REVIEW_REQUIRED

The engine is **not a UI** — it is a pure Python calculation backend. A separate Streamlit UI (to be built) will call it and display results to agency desk officers.

---

## Project Structure

```
vendy-process/
├── core/
│   ├── port_router.py          # Main entry point — route() function
│   ├── output_formatter.py     # Output schema (dataclasses + to_dict())
│   ├── surcharge_engine.py     # Applies multiplier surcharges
│   ├── overtime_calculator.py  # Calculates overtime charges
│   ├── pdf_extractor.py        # PDF/OCR utilities (not yet integrated)
│   ├── semantic_matcher.py     # ChromaDB semantic search (not yet integrated)
│   └── handlers/
│       ├── bracket_lookup.py               # LOA/GT → rate table lookup
│       ├── bracket_lookup_with_formula.py  # Fixed bracket + formula above threshold (Ceuta)
│       ├── fixed_plus_variable.py          # Rate = Fixed + (Variable × GT)
│       ├── hp_hourly.py                    # Rate = HP × hours × rate_per_HP (Mexico)
│       ├── hourly_with_minimum.py          # Hourly rate with minimum floor (Panama)
│       └── per_service_tug_count.py        # Fixed rate × tug count
├── tariffs/                    # One JSON per port (25 files)
├── calculation_profiles.json   # Per-port engine configuration
├── run_test_*.py               # Per-port test scripts (one per tested port)
├── test_sof_*.json             # SOF test data
├── test_invoice_*.json         # Invoice test data
└── requirements.txt            # streamlit, pdfplumber, chromadb, etc.
```

---

## Core API — The `route()` Function

**File:** `core/port_router.py`

```python
from port_router import route

result = route(
    port="Algeciras",           # Must match a key in calculation_profiles.json
    sof_data=sof_dict,          # SOF JSON (see shape below)
    invoice_lines=line_list,    # List of service line item dicts (exclude adjustment lines)
    tariff_data=tariff_dict,    # Contents of tariffs/<port>.json
    calculation_profiles=profiles_dict,  # Contents of calculation_profiles.json
    invoice_reference="INV-001",
    vendor="Boluda Spain",
    vessel_name="STI HAMMERSMITH",
    service_date="2026-01-10",  # ISO date string, used to filter SOF events
    match_tolerance_pct=1.0,    # Variance % within which MATCH is declared
)
# result is a dict (from to_dict(FormattedResult))
```

**To call the engine, you need to load four JSON files and call route().** That's the entire API surface.

---

## Output Schema

`route()` returns a plain `dict` (via `to_dict()`). Full structure:

```json
{
  "invoice_reference": "0260040235",
  "vendor": "Boluda Towage Spain",
  "port": "Cadiz_Bay",
  "vessel_name": "STI HAMMERSMITH",
  "vessel_dimension_type": "GT",
  "vessel_dimension_value": 24230.0,
  "service_date": "2026-01-10",
  "currency": "EUR",
  "validated_at": "2026-01-24T10:00:00+00:00",

  "overall_verdict": "AUTO_APPROVED",   // "AUTO_APPROVED" | "MISMATCH" | "REVIEW_REQUIRED"
  "overall_confidence": 0.85,           // 0.0 – 1.0
  "overall_confidence_label": "MEDIUM", // "HIGH" | "MEDIUM" | "LOW"
  "total_expected": 7413.48,
  "total_invoiced": 7503.11,
  "total_variance": 89.63,
  "human_review_required": false,
  "summary_notes": ["Line 1: +1.21% variance — annual tariff update likely"],

  "line_items": [
    {
      "line_number": 1,
      "service_description": "Berth",
      "sof_event_cited": "SOF event: Berth at 21:20",
      "tariff_rule_cited": "Cadiz_Bay tariff — pattern: fixed_plus_variable, zone: other_docks",
      "expected_amount": 7413.48,
      "invoiced_amount": 7503.11,
      "currency": "EUR",
      "variance": 89.63,
      "variance_pct": 1.21,
      "verdict": "MISMATCH",           // "MATCH" | "MISMATCH" | "UNSUPPORTED" | "REVIEW"
      "confidence_score": 0.85,
      "confidence_label": "MEDIUM",
      "handler_used": "fixed_plus_variable",
      "surcharges_applied": [],         // list of {name, multiplier, amount, citation}
      "overtime_applied": null,         // string citation or null
      "human_review_flag": false,
      "human_review_reason": "",
      "notes": "",
      "candidate_explanations": []      // list of {type, description, expected_amount, variance_pct}
    }
  ]
}
```

### Verdict definitions
| Verdict | Meaning |
|---------|---------|
| `AUTO_APPROVED` | All lines MATCH within tolerance, no flags |
| `MISMATCH` | At least one line variance exceeds `match_tolerance_pct` |
| `REVIEW_REQUIRED` | At least one REVIEW or UNSUPPORTED line |
| `MATCH` (line-level) | Variance within tolerance |
| `MISMATCH` (line-level) | Variance outside tolerance |
| `UNSUPPORTED` | No SOF event found to support this charge |
| `REVIEW` | Engine flagged for human review (e.g. open tariff doubt) |

### Confidence scoring
Score starts at 1.0, deductions applied:
- No SOF event found: −0.30
- Human review flag: −0.15
- Tug count from invoice only: −0.05
- Zone inferred: −0.05

HIGH ≥ 0.90 / MEDIUM ≥ 0.70 / LOW < 0.70

---

## SOF Input Shape

The `sof_data` dict must have this structure:

```json
{
  "vessel": {"name": "STI HAMMERSMITH", "imo": "9706463", "gt": 24230},
  "port": "Cadiz",
  "events": [
    {
      "type": "Berth",
      "service_type": "Berth",
      "date": "2026-01-10",
      "tugs_alongside_time": "21:20",
      "all_fast_time": "22:30",
      "tug_count": 3,
      "berth_location": "South Jetty",
      "duration_mins": 70
    }
  ]
}
```

Events are matched to invoice lines by `service_type` ("Berth" or "Unberth") and `date` (must match `service_date` passed to `route()`).

**Multi-vessel SOFs** (where a SOF covers multiple ships) are handled: the engine calls `_resolve_sof_for_vessel()` to find the sub-section matching the invoice vessel name.

---

## Invoice Line Item Shape

Each dict in `invoice_lines` must include:

```json
{
  "service_type": "Berth",          // "Berth" | "Unberth" | "Shifting" etc.
  "description": "Atraque 10.01.2026",
  "date": "2026-01-10",
  "amount": 7503.11,                 // invoiced amount for this line
  "gt": "24,230",                    // vessel GT (string OK; European format handled)
  "loa": 184.0,                      // vessel LOA in meters (for LOA-based ports)
  "tug_count": 3,                    // number of tugs
  "zone": "other_docks_excluding_santa_maria"  // port-specific zone key
}
```

**Number format note:** GT/LOA strings in European format are auto-normalised:
- `"24.230"` → 24,230 GT (dot + 3 digits = thousands separator)
- `"23,403"` → 23,403 GT
- `"23.40"` → 23.40 (dot + 1–2 digits = genuine decimal)

**Adjustment lines** (bunker surcharges, discounts) must be filtered out before passing to `route()`:
```python
service_lines    = [l for l in invoice["line_items"] if not l.get("is_adjustment")]
adjustment_lines = [l for l in invoice["line_items"] if l.get("is_adjustment")]
result = route(..., invoice_lines=service_lines, ...)
```

---

## Calculation Patterns (by Port)

| Pattern | Ports | Dimension |
|---------|-------|-----------|
| `fixed_plus_variable` | Algeciras, Valencia, Huelva, Cadiz Bay, Tenerife/La Palma, Castellon, Las Palmas | GT |
| `bracket_lookup` | Ghent, Le Havre, Antwerp, Rotterdam, Dordrecht/Moerdijk, Rostock, Brake, Altamira, Guaymas, Manzanillo, Salina Cruz, Tampico, Santo Domingo/Haina | LOA or GT |
| `bracket_lookup_with_formula_above_threshold` | Ceuta | GT |
| `hp_hourly` | Mazatlan, Coatzacoalcos (MXN → USD via invoice FX rate) | GRT |
| `hourly_with_minimum` | Panama | none (flat hourly) |
| `per_service_tug_count_specific` | Ensenada | GRT |

**Billing basis** (affects whether rate is multiplied by tug count):
- `per_service` — one rate per maneuver regardless of tug count (most Spanish ports)
- `per_tug_per_move` — rate × tug count (Le Havre, Ghent, Antwerp, Rotterdam)
- `per_service_per_tug` — Ceuta
- `per_hp_per_hour` — Mexico HP-based ports

---

## Port Testing Status (as of session ending)

| Port | Status | Pattern | Notes |
|------|--------|---------|-------|
| Algeciras | ✅ TESTED | fixed_plus_variable | Baseline, always run as regression check |
| Valencia | ✅ TESTED | fixed_plus_variable | |
| Ghent | ✅ TESTED | bracket_lookup | |
| Antwerp | ✅ TESTED | bracket_lookup | |
| Rotterdam | ✅ TESTED | bracket_lookup | |
| Rostock | ✅ TESTED | bracket_lookup | |
| Le Havre | ✅ TESTED | bracket_lookup | TABLE_A/B/C/D; bunker surcharge = adjustment line |
| Mazatlan | ✅ TESTED | hp_hourly | MXN/USD FX conversion per line |
| Huelva | ✅ TESTED | fixed_plus_variable | |
| Ceuta | ✅ TESTED | bracket_lookup_with_formula | T0/T±1/T±2 tables; displacement fee; bay area surcharge |
| Cadiz Bay | ✅ TESTED | fixed_plus_variable | geographic_sections_and_terminals zone structure |
| Tenerife/La Palma | ✅ TESTED | fixed_plus_variable | tariff_matrix_consolidated; zone = nested {fixed, variable} dict |
| Dordrecht/Moerdijk | ✅ TESTED | bracket_lookup | MISMATCH −9.09% (2026 tariff on file; service July 2025) |
| Santo Domingo/Haina | ⏳ pending | bracket_lookup | |
| Brake | ⏳ pending | bracket_lookup | |
| Altamira | ⏳ pending | bracket_lookup | |
| Coatzacoalcos | ⏳ pending | hp_hourly | |
| Ensenada | ✅ TESTED | per_service_tug_count_specific | AUTO_APPROVED; MXN→USD FX conversion added to dispatch; GRT 4,000 departure 1 tug |
| Guaymas | ⏳ pending | bracket_lookup | |
| Manzanillo | ⏳ pending | bracket_lookup | |
| Salina Cruz | ⏳ pending | bracket_lookup | |
| Tampico | ✅ TESTED | bracket_lookup | MISMATCH −10.59% (Apr 2025 tariff on file; invoice Feb 2025 pre-April rates) |
| Panama | ⏳ pending | hourly_with_minimum | |
| Castellon | ⏳ pending | fixed_plus_variable | |
| Las Palmas | ✅ TESTED | fixed_plus_variable | fixed_hourly_amount OT handler; T+1 tariff +0.61% → AUTO_APPROVED |

Run regression check after any engine change:
```bash
python run_test.py          # Algeciras (baseline)
python run_test_tenerife.py # representative fixed_plus_variable with zone columns
python run_test_le_havre.py # representative bracket_lookup with per_tug billing
```

---

## Loading the Engine from a UI

The simplest integration for a Streamlit UI:

```python
import sys, json, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent / "core"))
from port_router import route

def run_verification(port, sof_dict, invoice_dict, tariff_dict, profiles_dict):
    service_lines = [l for l in invoice_dict["line_items"] if not l.get("is_adjustment")]
    return route(
        port=port,
        sof_data=sof_dict,
        invoice_lines=service_lines,
        tariff_data=tariff_dict,
        calculation_profiles=profiles_dict,
        invoice_reference=invoice_dict.get("invoice_reference", ""),
        vendor=invoice_dict.get("vendor", ""),
        vessel_name=invoice_dict.get("vessel_name", ""),
        service_date=invoice_dict.get("service_date", ""),
        match_tolerance_pct=1.0,
    )
```

Tariff files live in `tariffs/`. Profile key format = port name with underscores (e.g. `"Le_Havre"`, `"Tenerife_La_Palma"`). Use `calculation_profiles["calculation_profiles"][port_key]` to check a port is configured before calling.

---

## Key Design Decisions

- **GT always from invoice** — if SOF and invoice disagree on GT, invoice value is used
- **Duration always from SOF** — SOF event `duration_mins` is authoritative
- **SOF date filtering** — only events whose `date` matches `service_date` are matched to invoice lines
- **Tariff year gap** — tariff JSONs are dated 2025; some 2026 invoices show ~1–2% higher rates. This consistently produces MISMATCH at ~1.2%. This is expected and documented in test notes
- **Contract discounts** — deferred; when Geoconnect contracts arrive, add `contract_discount_pct` to all port profiles simultaneously
- **No VAT calculation** — all ports use reverse charge (IVA/IGIC no sujeto). VAT lines filtered or ignored
- **Adjustment lines** — bunker surcharges, fuel adjustments, discounts are `is_adjustment: true` and excluded from `route()`. They are printed separately in test scripts but not validated
- **`candidate_explanations`** — when a MISMATCH occurs, the engine tries to explain it via ratio-based inference (zone variants, known surcharges, waiting time, delay discounts). Each explanation has `{type, description, expected_amount, variance_pct}`

---

## UI Requirements (for the Streamlit desk officer UI)

The UI should allow an agency desk officer to:

1. **Input**: Upload or paste invoice JSON + SOF JSON, select port, load tariff automatically
2. **Run**: Call `route()` and display results
3. **Invoice header**: Show vessel, port, vendor, invoice ref, service date, currency
4. **Overall verdict banner**: AUTO_APPROVED (green) / MISMATCH (red) / REVIEW_REQUIRED (amber)
5. **Line item table**: One row per line — service description, expected, invoiced, variance %, verdict badge
6. **Confidence indicator**: Score + label per line and overall
7. **Audit trail expandable**: `sof_event_cited` + `tariff_rule_cited` per line
8. **Candidate explanations**: If MISMATCH, show possible explanations from `candidate_explanations`
9. **Adjustment lines**: Display separately (not validated, shown for completeness)
10. **Approve / Escalate buttons**: Officer action — `AUTO_APPROVED` lines can be approved in bulk; MISMATCH/REVIEW lines require explicit officer decision

The UI is **read-only** with respect to the engine — it calls `route()` and displays results. It does not modify tariff files or profiles.

---

## Running Tests

```bash
cd C:/Scorpio/vendy-process
python run_test.py                # Algeciras
python run_test_le_havre.py       # Le Havre
python run_test_tenerife.py       # Tenerife
python run_test_mazatlan.py       # Mazatlan
python run_test_huelva.py         # Huelva
python run_test_ceuta.py          # Ceuta
python run_test_cadiz_bay.py      # Cadiz Bay
# ... etc for each port
```

All test scripts follow the same pattern: load 4 JSON files → call `route()` → print result.
