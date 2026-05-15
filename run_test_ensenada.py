"""
run_test_ensenada.py
---------------------
Test: PACIFIC GLORY at Port of Ensenada (ENS-2025-0815)
GRT: 4,000  FX: 17.50 MXN/USD
Unberth (departure): MXN 43,436 / 17.50 = USD 2,481.49
Duration: 45 min (< 1hr standard — no overtime)
Expected: AUTO_APPROVED
"""

import json
import pathlib
import pprint

from core.port_router import route

BASE = pathlib.Path(__file__).parent

with open(BASE / "test_sof_ensenada.json", encoding="utf-8") as f:
    sof = json.load(f)

with open(BASE / "test_invoice_ensenada.json", encoding="utf-8") as f:
    invoice = json.load(f)

with open(BASE / "tariffs" / "ensenada.json", encoding="utf-8") as f:
    tariff = json.load(f)

with open(BASE / "calculation_profiles.json", encoding="utf-8") as f:
    profiles = json.load(f)

service_lines    = [l for l in invoice["line_items"] if not l.get("is_adjustment")]
adjustment_lines = [l for l in invoice["line_items"] if l.get("is_adjustment")]

result = route(
    port="Ensenada",
    sof_data=sof,
    invoice_lines=service_lines,
    tariff_data=tariff,
    calculation_profiles=profiles,
    invoice_reference=invoice.get("invoice_reference", ""),
    vendor=invoice.get("vendor", ""),
    vessel_name=invoice.get("vessel_name", ""),
    service_date=invoice.get("service_date", ""),
    match_tolerance_pct=1.0,
)

print("=" * 60)
print(f"Invoice    : {result['invoice_reference']}")
print(f"Vessel     : {result['vessel_name']}")
print(f"Port       : {result['port']}")
print(f"Verdict    : {result['overall_verdict']}")
print(f"Currency   : {result['currency']}")
print(f"Total Exp  : {result['total_expected']:,.2f}")
print(f"Total Inv  : {result['total_invoiced']:,.2f}")
print(f"Variance   : {result['total_variance']:,.2f}")
print()
for li in result["line_items"]:
    print(f"  Line {li['line_number']}: {li['service_description']}")
    print(f"    Expected : {li['expected_amount']:,.2f}  |  Invoiced: {li['invoiced_amount']:,.2f}")
    print(f"    Variance : {li['variance_pct']:.2f}%  |  Verdict: {li['verdict']}")
    if li.get("overtime_applied"):
        print(f"    OT       : {li['overtime_applied']}")
    if li.get("notes"):
        print(f"    Notes    : {li['notes']}")
print()

if adjustment_lines:
    adj_total = sum(l["amount"] for l in adjustment_lines)
    print(f"Adjustment lines (not validated):")
    for adj in adjustment_lines:
        print(f"  {adj['description']}: {adj['amount']:,.2f} USD")
    print(f"  Adjustment total: {adj_total:,.2f} USD")
    print()

print("Full result:")
pprint.pprint(result)
