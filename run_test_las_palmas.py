"""
run_test_las_palmas.py
----------------------
Test: HAFNIA SOYA at Las Palmas (Invoice 0480038376)
GT: 24,120  Currency: EUR  Tariff: T+1 (~+0.614% above 2025 T tariff)

Tariff lookup (T, 2025):
  GT 24,120 -> bracket "up_to_gt: 25,000" -> fixed=955.36, variable=0.10181173
  Rate = 955.36 + (0.10181173 x 24,120) = EUR 3,411.06 per service

Berth (24.02.2026): 2 tugs, 78 min -> 18 min OT -> 1 full hr billed
  Base: EUR 3,411.06
  OT:   2 tugs x EUR 1,560.89 = EUR 3,121.78
  Expected: EUR 6,532.84  |  Invoiced: EUR 6,572.94  (+0.614% T+1) -> MATCH

Unberth (26.02.2026): 2 tugs, 24 min <= 60 min std -> no OT
  Expected: EUR 3,411.06  |  Invoiced: EUR 3,432.00  (+0.614% T+1) -> MATCH

Expected: AUTO_APPROVED (both lines within 1% tolerance via T+1 tariff)
"""

import json
import pathlib
import pprint

from core.port_router import route

BASE = pathlib.Path(__file__).parent

with open(BASE / "test_sof_las_palmas_hafnia.json", encoding="utf-8") as f:
    sof = json.load(f)

with open(BASE / "test_invoice_las_palmas_hafnia.json", encoding="utf-8") as f:
    invoice = json.load(f)

with open(BASE / "tariffs" / "las_palmas.json", encoding="utf-8") as f:
    tariff = json.load(f)

with open(BASE / "calculation_profiles.json", encoding="utf-8") as f:
    profiles = json.load(f)

service_lines    = [l for l in invoice["line_items"] if not l.get("is_adjustment")]
adjustment_lines = [l for l in invoice["line_items"] if l.get("is_adjustment")]

result = route(
    port="Las_Palmas",
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
    print(f"    Variance : {li['variance_pct']:.3f}%  |  Verdict: {li['verdict']}")
    if li.get("overtime_applied"):
        print(f"    OT       : {li['overtime_applied']}")
print()

print("Full result:")
pprint.pprint(result)
