import sys, json, pathlib

sys.path.insert(0, str(pathlib.Path(__file__).parent / "core"))
from port_router import route

BASE = pathlib.Path(__file__).parent

sof      = json.loads((BASE / "test_sof_santo_domingo.json").read_text())
invoice  = json.loads((BASE / "test_invoice_santo_domingo.json").read_text())
tariff   = json.loads((BASE / "tariffs" / "santo_domingo_haina.json").read_text())
profiles = json.loads((BASE / "calculation_profiles.json").read_text())

service_lines    = [l for l in invoice["line_items"] if not l.get("is_adjustment")]
adjustment_lines = [l for l in invoice["line_items"] if l.get("is_adjustment")]

result = route(
    port="Santo_Domingo_Haina",
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
print(f"Vessel:          {result['vessel_name']}  GT {result['vessel_dimension_value']}")
print(f"Invoice:         {result['invoice_reference']}")
print(f"Currency:        {result['currency']}")
print(f"Overall Verdict: {result['overall_verdict']}")
print(f"Total Expected:  {result['total_expected']}")
print(f"Total Invoiced:  {result['total_invoiced']}")
print(f"{'='*60}\n")

for li in result["line_items"]:
    print(f"  Line {li['line_number']}: {li['service_description']}")
    print(f"    Expected: {li['expected_amount']}  Invoiced: {li['invoiced_amount']}")
    print(f"    Variance: {li['variance_pct']}%   Verdict: {li['verdict']}")
    print(f"    SOF:      {li['sof_event_cited']}")
    print()

if adjustment_lines:
    print("  -- Adjustment lines (informational) --")
    ot_total = sum(a['amount'] for a in adjustment_lines if 'Overtime' in a.get('service_type','') or 'Overtime' in a.get('description',''))
    bk_total = sum(a['amount'] for a in adjustment_lines if 'Bunker' in a.get('service_type','') or 'Bunker' in a.get('description',''))
    for al in adjustment_lines:
        print(f"    {al['description']}: USD {al['amount']:.2f}")
    print(f"\n  Overtime subtotal:       USD {ot_total:.2f}  (prorata billing — not validated)")
    print(f"  Bunker surcharge total:  USD {bk_total:.2f}  (variable — accepted as invoiced)")
    svc_total = sum(l['amount'] for l in service_lines)
    print(f"\n  Service lines total:     USD {svc_total:.2f}")
    print(f"  Grand total invoice:     USD {svc_total + ot_total + bk_total:.2f}")
