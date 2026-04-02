"""
run_test_manzanillo.py
----------------------
Test: SILVER VALERIE at Port of Manzanillo (MAN5180044603)
GRT: 29,460  FX: 17.9792 MXN/USD (DOF 19.12.2025)

Tariff lookup:
  GRT 29,460 → bracket "Over 20,000 Tons" → basic_quota MXN 88,607
  88,607 / 17.9792 = USD 4,928.32 per service (per_service billing)

Berth  (20.12.2025): 2 tugs, 60 min ≤ 60 min std → no OT → USD 4,928.32
Unberth(21.12.2025): 2 tugs, 40 min ≤ 60 min std → no OT → USD 4,928.32

Expected: AUTO_APPROVED (both lines exact match)

Adjustment lines (not validated by engine):
  10% contract discount: −USD 492.84 × 2 = −USD 985.68
  Mexican IVA (VAT) 16%: +USD 1,419.36
  Grand total (incl. VAT): USD 10,290.32
"""

import sys
import json
import pathlib
import pprint

sys.path.insert(0, str(pathlib.Path(__file__).parent / "core"))
from port_router import route

BASE = pathlib.Path(__file__).parent

with open(BASE / "test_sof_manzanillo.json", encoding="utf-8") as f:
    sof = json.load(f)

with open(BASE / "test_invoice_manzanillo.json", encoding="utf-8") as f:
    invoice = json.load(f)

with open(BASE / "tariffs" / "manzanillo.json", encoding="utf-8") as f:
    tariff = json.load(f)

with open(BASE / "calculation_profiles.json", encoding="utf-8") as f:
    profiles = json.load(f)

service_lines    = [l for l in invoice["line_items"] if not l.get("is_adjustment")]
adjustment_lines = [l for l in invoice["line_items"] if l.get("is_adjustment")]

result = route(
    port="Manzanillo",
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

print("=" * 62)
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
print()

if adjustment_lines:
    adj_total = sum(l["amount"] for l in adjustment_lines)
    print("Adjustment lines (not validated):")
    for adj in adjustment_lines:
        print(f"  {adj['description']}: {adj['amount']:,.2f} USD")
    svc_total = result["total_invoiced"]
    print(f"  Adjustment total       : {adj_total:,.2f} USD")
    print(f"  Grand total (svc + adj): {svc_total + adj_total:,.2f} USD")
    print(f"  (Invoice total incl. VAT: USD 10,290.32)")

print()
print("Full result:")
pprint.pprint(result)
