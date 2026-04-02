import sys, json, pathlib, pprint

sys.path.insert(0, str(pathlib.Path(__file__).parent / "core"))
from port_router import route

BASE = pathlib.Path(__file__).parent

sof      = json.loads((BASE / "test_sof_tenerife.json").read_text())
invoice  = json.loads((BASE / "test_invoice_tenerife.json").read_text())
tariff   = json.loads((BASE / "tariffs" / "tenerife_la_palma.json").read_text())
profiles = json.loads((BASE / "calculation_profiles.json").read_text())

service_lines = [l for l in invoice["line_items"] if not l.get("is_adjustment")]

result = route(
    port="Tenerife_La_Palma",
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
