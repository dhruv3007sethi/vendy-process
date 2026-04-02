"""
run_test_tampico.py
-------------------
Test: STI LA BOCA at Port of Tampico (TMP5170036247 / TMP5170036248)
GRT: 29,738

Tariff (April 2025, effective 2025-04-20):
  GRT 29,738 -> bracket "Over 15,001" -> rate (MXN): 107,965

Berth (06.02.2025): 2 tugs, FX 20.4268 MXN/USD
  Actual: 97 min (VB CARIBE 19:38-21:15)
  OT: 37 min -> 3 x 15-min increments -> 25% x 3 = 0.75 x base
  Expected = 107,965/20.4268 x 1.75 = ~USD 9,249.56
  Invoiced = USD 8,269.66  (~-10.6%)

Unberth (15.02.2025): 2 tugs, FX 20.4978 MXN/USD
  Actual: 40 min (VB SONORA 12:00-12:40) -> no OT (< 60 min std)
  Expected = 107,965/20.4978 = ~USD 5,267.15
  Invoiced = USD 4,709.14  (~-10.6%)

Expected: MISMATCH (~-10.6% both lines)
Cause: April 2025 tariff on file; invoice used pre-April 2025 (February 2025) rates
"""

import sys
import json
import pathlib
import pprint

sys.path.insert(0, str(pathlib.Path(__file__).parent / "core"))
from port_router import route

BASE = pathlib.Path(__file__).parent

with open(BASE / "test_sof_tampico.json", encoding="utf-8") as f:
    sof = json.load(f)

with open(BASE / "test_invoice_tampico.json", encoding="utf-8") as f:
    invoice = json.load(f)

with open(BASE / "tariffs" / "tampico.json", encoding="utf-8") as f:
    tariff = json.load(f)

with open(BASE / "calculation_profiles.json", encoding="utf-8") as f:
    profiles = json.load(f)

service_lines    = [l for l in invoice["line_items"] if not l.get("is_adjustment")]
adjustment_lines = [l for l in invoice["line_items"] if l.get("is_adjustment")]

result = route(
    port="Tampico",
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
    print(f"  Adjustment total        : {adj_total:,.2f} USD")
    print(f"  Grand total (svc + adj) : {svc_total + adj_total:,.2f} USD")
    print(f"  (Invoice grand total incl. discount+VAT: USD 13,549.87)")

print()
print("NOTE: April 2025 tariff on file (effective 2025-04-20).")
print("      Invoice dated Feb 2025 used pre-April rates (~10.6% lower than tariff).")
print()
print("Full result:")
pprint.pprint(result)
