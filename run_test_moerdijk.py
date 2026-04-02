import sys, json, pathlib

sys.path.insert(0, str(pathlib.Path(__file__).parent / "core"))
from port_router import route

BASE = pathlib.Path(__file__).parent

sof      = json.loads((BASE / "test_sof_moerdijk.json").read_text())
invoice  = json.loads((BASE / "test_invoice_moerdijk.json").read_text())
tariff   = json.loads((BASE / "tariffs" / "dordrecht_moerdijk.json").read_text())
profiles = json.loads((BASE / "calculation_profiles.json").read_text())

service_lines    = [l for l in invoice["line_items"] if not l.get("is_adjustment")]
adjustment_lines = [l for l in invoice["line_items"] if l.get("is_adjustment")]

result = route(
    port="Dordrecht_Moerdijk",
    sof_data=sof,
    invoice_lines=service_lines,
    tariff_data=tariff,
    calculation_profiles=profiles,
    invoice_reference=invoice["invoice_reference"],
    vendor=invoice["vendor"],
    vessel_name=invoice["vessel_name"],
    service_date=invoice["service_date"],
    match_tolerance_pct=1.0,
)

print(f"\n{'='*60}")
print(f"Port:            {result['port']}")
print(f"Vessel:          {result['vessel_name']}  LOA {result['vessel_dimension_value']}m")
print(f"Invoice:         {result['invoice_reference']}")
print(f"Overall Verdict: {result['overall_verdict']}")
print(f"Total Expected:  {result['total_expected']}")
print(f"Total Invoiced:  {result['total_invoiced']}")
print(f"Total Variance:  {result['total_variance']}")
print(f"{'='*60}\n")

for li in result["line_items"]:
    print(f"  Line {li['line_number']}: {li['service_description']}")
    print(f"    Expected: {li['expected_amount']}  Invoiced: {li['invoiced_amount']}")
    print(f"    Variance: {li['variance_pct']}%   Verdict: {li['verdict']}")
    print(f"    Handler:  {li['handler_used']}")
    print(f"    SOF:      {li['sof_event_cited']}")
    print(f"    Tariff:   {li['tariff_rule_cited']}")
    if li.get("notes"):
        print(f"    Notes:    {li['notes']}")
    print()

if adjustment_lines:
    print("  -- Adjustment lines (informational) --")
    for al in adjustment_lines:
        print(f"    {al['description']}: EUR {al['amount']:.2f}")
print()
print("NOTE: 2026 tariff on file (effective 2026-01-01); service date 25.07.2025 uses 2025 rates.")
print("      Engine expects EUR 3,520 (section_6 up_to_138m, 2026 tariff); invoice shows EUR 3,200 (2025 rate).")
