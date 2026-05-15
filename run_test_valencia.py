import json, pathlib, logging

logging.basicConfig(level=logging.WARNING)

from core.port_router import route

sof      = json.loads(pathlib.Path("test_sof_valencia.json").read_text())
invoice  = json.loads(pathlib.Path("test_invoice_valencia.json").read_text())
tariff   = json.loads(pathlib.Path("tariffs/valencia.json").read_text())
profiles = json.loads(pathlib.Path("calculation_profiles.json").read_text())

result = route(
    port="Valencia",
    sof_data=sof,
    invoice_lines=invoice["line_items"],
    tariff_data=tariff,
    calculation_profiles=profiles,
    invoice_reference=invoice["invoice_reference"],
    vendor=invoice["vendor"],
    vessel_name=invoice["vessel_name"],
    service_date=invoice["service_date"],
    match_tolerance_pct=1.0,
)

print(json.dumps(result, indent=2, ensure_ascii=False))
