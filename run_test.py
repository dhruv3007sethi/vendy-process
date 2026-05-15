"""
run_test.py
-----------
Test runner for the Algeciras calculation engine.
Uses HAFNIA SOYA invoice (0260040937) against SOF and tariff data.
"""

import json

from core.port_router import route

# Load data files
with open("SOF.json") as f:
    sof = json.load(f)

with open("invoice_1.json") as f:
    invoice = json.load(f)

with open("tariffs/algeciras.json") as f:
    tariff = json.load(f)

with open("calculation_profiles.json") as f:
    profiles = json.load(f)

# Inject invoice-level GT into each line item so the engine uses the vendor's GT
invoice_gt = invoice.get("gross_tonnage")
invoice_lines = []
for line in invoice.get("line_items", []):
    line_copy = dict(line)
    if invoice_gt and "gt" not in line_copy:
        line_copy["gt"] = invoice_gt
    invoice_lines.append(line_copy)

# Run the engine
result = route(
    port="Algeciras",
    sof_data=sof,
    invoice_lines=invoice_lines,
    tariff_data=tariff,
    calculation_profiles=profiles,
    invoice_reference=invoice.get("invoice_number", ""),
    vendor=invoice.get("vendor_name", ""),
    vessel_name=invoice.get("vessel_name", ""),
    service_date=invoice.get("date_of_issue", ""),
    match_tolerance_pct=1.0
)

print(json.dumps(result, indent=2))
