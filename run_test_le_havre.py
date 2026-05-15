import json, pathlib, logging

logging.basicConfig(level=logging.WARNING)

from core.port_router import route

sof      = json.loads(pathlib.Path("test_sof_le_havre.json").read_text())
invoice  = json.loads(pathlib.Path("test_invoice_le_havre.json").read_text())
tariff   = json.loads(pathlib.Path("tariffs/le_havre.json").read_text())
profiles = json.loads(pathlib.Path("calculation_profiles.json").read_text())

service_lines    = [l for l in invoice["line_items"] if not l.get("is_adjustment")]
adjustment_lines = [l for l in invoice["line_items"] if l.get("is_adjustment")]

result = route(
    port="Le_Havre",
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
print("=== ADJUSTMENT LINES (not tariff-validated) ===")
for a in adjustment_lines:
    print(f"  {a['description']}: EUR {a['amount']:,.2f}")
    print(f"    note: {a['note']}")
print()
print("=== NOTES ===")
for n in invoice.get("notes", []):
    print(f"  - {n}")
