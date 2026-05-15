import json, pathlib, logging

logging.basicConfig(level=logging.WARNING)

from core.port_router import route

sof      = json.loads(pathlib.Path("test_sof_ghent.json").read_text())
invoice  = json.loads(pathlib.Path("test_invoice_ghent.json").read_text())
tariff   = json.loads(pathlib.Path("tariffs/ghent.json").read_text())
profiles = json.loads(pathlib.Path("calculation_profiles.json").read_text())

# Only pass tug service lines to the engine (not adjustment lines)
service_lines = [l for l in invoice["line_items"] if not l.get("is_adjustment")]
adjustment_lines = [l for l in invoice["line_items"] if l.get("is_adjustment")]

result = route(
    port="Ghent",
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

print(json.dumps(result, indent=2, ensure_ascii=False))
print()
print("=== ADJUSTMENT LINES (outside tariff scope — human review required) ===")
for a in adjustment_lines:
    print(f"  {a['description']}: EUR {a['amount']:,.2f} — {a['note']}")
print()
print("=== NOTES ===")
for n in invoice.get("notes", []):
    print(f"  - {n}")
