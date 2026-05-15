"""
core/invoice_normaliser.py

Normalise raw invoice and SOF JSON (from ChatGPT / NotebookLM / manual entry)
into the flat dicts expected by `port_router.route()`.

Public API:
    normalise_invoice(inv)          → flat invoice dict
    normalise_invoice_fields(inv)   → (reference, vendor, vessel, date)
    normalise_sof(sof)              → engine-format SOF dict
    prepare_lines(inv)              → (service_lines, adjustment_lines)
    is_adjustment_line(desc, line)  → bool
    infer_service_type(desc)        → str
"""

import re


# ── Service-type inference ─────────────────────────────────────────

def infer_service_type(description: str) -> str:
    """Infer Berth / Unberth / Shifting from free-text description."""
    d = description.lower()
    if any(w in d for w in ("arrival", "inward", "atraque", "berthing", "mooring", "berth")):
        return "Berth"
    if any(w in d for w in ("departure", "outward", "desatraque", "unberth", "unberthing", "sailing")):
        return "Unberth"
    if any(w in d for w in ("shifting", "shift", "cambio")):
        return "Shifting"
    return "Berth"


# ── Adjustment-line detection ──────────────────────────────────────

_ADJ_RE = re.compile(
    r"\b(?:discount|rebate|bunker|surcharge|vat|tax|iva|igic"
    r"|baf|fuel|adjustment|korting|remise"
    r"|btw|mwst|tva)\b"
)


def is_adjustment_line(description: str, line: dict) -> bool:
    """Return True for discount, surcharge, VAT, bunker lines."""
    is_adj = line.get("is_adjustment")
    if is_adj is True:
        return True
    if is_adj is False:
        return False
    return bool(_ADJ_RE.search(description.lower()))


# ── Date extraction ────────────────────────────────────────────────

_SOF_MONTH_MAP = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04",
    "may": "05", "jun": "06", "jul": "07", "aug": "08",
    "sep": "09", "oct": "10", "nov": "11", "dec": "12",
}


def _extract_date(raw: str) -> str:
    """Convert common date formats to YYYY-MM-DD. Returns raw string if unparseable."""
    if not raw:
        return ""
    raw = str(raw).strip()
    if re.match(r"\d{4}-\d{2}-\d{2}", raw):
        return raw[:10]
    m = re.match(r"(\d{1,2})[-/\s]([A-Za-z]{3})[-/\s](\d{4})", raw)
    if m:
        d, mon, y = m.groups()
        mo = _SOF_MONTH_MAP.get(mon.lower(), "01")
        return f"{y}-{mo}-{d.zfill(2)}"
    m = re.match(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", raw)
    if m:
        return f"{m.group(3)}-{m.group(2).zfill(2)}-{m.group(1).zfill(2)}"
    return raw


# ── SOF event-type inference ───────────────────────────────────────

def _infer_sof_event_type(event_text: str) -> str:
    """Map free-text SOF event description to Berth / Unberth / Shifting."""
    t = event_text.lower()
    if any(w in t for w in ("all fast", "first line", "made fast", "alongside", "berthing", "arrival", "mooring")):
        return "Berth"
    if any(w in t for w in ("last line", "departure", "unberth", "cast off", "let go", "sailed", "leaving")):
        return "Unberth"
    if any(w in t for w in ("shift", "warp")):
        return "Shifting"
    return ""


# ── SOF normalisation ─────────────────────────────────────────────

def normalise_sof(sof: dict) -> dict:
    """
    Accept any reasonable SOF JSON shape and return engine format.

    Handles ChatGPT SOF format differences:
      - vessel_details  instead of  vessel
      - event (free text) instead of type / service_type
      - date "07-Feb-2026" instead of "2026-02-07"
      - number_of_tugs  instead of  tug_count
    """
    if sof.get("vessel") and sof.get("events"):
        first_evt = sof["events"][0] if sof["events"] else {}
        if "type" in first_evt or "service_type" in first_evt:
            return sof

    vessel_raw = (
        sof.get("vessel_details") or
        sof.get("vessel") or
        sof.get("vessel_info") or {}
    )
    if not isinstance(vessel_raw, dict):
        vessel_raw = {}

    vessel_name = (
        vessel_raw.get("vessel_name") or vessel_raw.get("name") or
        sof.get("vessel_name") or ""
    )
    loa = vessel_raw.get("loa") or vessel_raw.get("loa_meters") or None
    gt  = vessel_raw.get("gt")  or vessel_raw.get("gross_tonnage") or None
    grt = vessel_raw.get("grt") or None

    raw_events = sof.get("events", [])
    norm_events = []
    for evt in raw_events:
        if not isinstance(evt, dict):
            continue
        event_text = (
            evt.get("event") or evt.get("type") or
            evt.get("service_type") or evt.get("description") or ""
        )
        svc_type   = evt.get("service_type") or evt.get("type") or _infer_sof_event_type(event_text)
        event_date = _extract_date(str(evt.get("date") or ""))
        tug_count  = evt.get("tug_count") or evt.get("number_of_tugs") or evt.get("tugs")

        ne: dict = {
            "type":             svc_type,
            "service_type":     svc_type,
            "event_description": event_text,
            "date":             event_date,
            "tug_count":        tug_count,
        }
        for k in ("time", "tugs_alongside_time", "all_fast_time",
                  "duration_mins", "duration_hrs", "berth_location"):
            if k in evt:
                ne[k] = evt[k]
        norm_events.append(ne)

    return {
        "vessel": {
            "name": vessel_name,
            **({"loa": loa} if loa is not None else {}),
            **({"gt": gt}   if gt  is not None else {}),
            **({"grt": grt} if grt is not None else {}),
        },
        "events": norm_events,
    }


# ── Invoice normalisation ─────────────────────────────────────────

def normalise_invoice(inv: dict) -> dict:
    """
    Accept any reasonable invoice JSON shape — engine flat format or raw
    ChatGPT/NotebookLM document format — and return the engine flat format.

    Handles nested structures like:
      inv.invoice_metadata.invoice_number
      inv.vessel_details.loa_meters / gross_tonnage
      inv.issuer.company  (vendor)
      inv.line_items[].unit_price_eur  (amount)
      inv.line_items[].details         (zone hint)
    """
    if inv.get("invoice_reference") and inv.get("line_items"):
        first = inv["line_items"][0] if inv["line_items"] else {}
        has_dim = first.get("loa") or first.get("gt") or first.get("grt")
        if has_dim and ("service_type" in first or "amount" in first):
            return inv

    meta = (
        inv.get("invoice_metadata") or inv.get("invoice_header") or
        inv.get("invoice_details") or inv.get("header") or {}
    )
    vessel  = inv.get("vessel_details") or inv.get("vessel") or {}
    issuer  = inv.get("issuer") or {}
    billing = inv.get("billing_details") or inv.get("billing_party") or {}
    totals  = inv.get("totals") or {}
    svc_blk = inv.get("service_details") or inv.get("service_info") or {}

    invoice_reference = (
        inv.get("invoice_reference") or
        meta.get("invoice_number") or
        meta.get("reference") or
        meta.get("invoice_ref") or
        ""
    )
    vendor = (
        inv.get("vendor") or
        issuer.get("company") or
        inv.get("vendor_name") or ""
    )
    vessel_name = (
        inv.get("vessel_name") or
        vessel.get("name") or vessel.get("vessel_name") or ""
    )

    raw_date = (
        inv.get("service_date") or
        svc_blk.get("service_date") or
        meta.get("service_date") or
        meta.get("invoice_date") or
        meta.get("date") or ""
    )
    service_date = _extract_date(raw_date)
    currency = inv.get("currency") or "EUR"

    _has_explicit_svc_date = bool(
        inv.get("service_date") or svc_blk.get("service_date") or meta.get("service_date")
    )
    if not _has_explicit_svc_date:
        raw_lines_preview = inv.get("line_items") or inv.get("charges") or inv.get("services") or []
        for _line in raw_lines_preview:
            _details = str(_line.get("details") or _line.get("detail") or "")
            _m = re.search(r"\b(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})\b", _details)
            if _m:
                _d_cand = f"{_m.group(3)}-{_m.group(2).zfill(2)}-{_m.group(1).zfill(2)}"
                if service_date and _d_cand < service_date:
                    service_date = _d_cand
                    break
                elif not service_date:
                    service_date = _d_cand
                    break

    _svc_zone_hint = (
        svc_blk.get("area") or svc_blk.get("zone") or svc_blk.get("location") or
        inv.get("zone") or ""
    )

    loa = (
        vessel.get("loa") or vessel.get("loa_meters") or vessel.get("loa_m") or
        vessel.get("length_overall") or inv.get("loa") or None
    )
    gt = (
        vessel.get("gt") or vessel.get("gross_tonnage") or vessel.get("grt") or
        inv.get("gt") or inv.get("gross_tonnage") or None
    )
    grt = vessel.get("grt") or inv.get("grt") or None

    raw_lines = inv.get("line_items") or inv.get("charges") or inv.get("services") or inv.get("items") or []
    normalised_lines = []
    for line in raw_lines:
        desc = (
            line.get("description") or
            line.get("service") or
            line.get("item") or ""
        )
        details = line.get("details") or line.get("detail") or ""
        amount = float(
            line.get("amount") or
            line.get("unit_price_eur") or
            line.get("amount_eur") or
            line.get("total") or 0.0
        )
        amount = abs(amount)

        line_type = str(line.get("type") or "").lower()
        if line_type in ("discount", "surcharge", "bunker", "adjustment", "vat", "tax"):
            line["is_adjustment"] = True
        is_adj = is_adjustment_line(desc, line)

        tug_count = (
            line.get("tug_count") or
            (int(line["quantity"]) if line.get("unit", "").lower() in ("tugs", "tug") else None)
        )

        zone = line.get("zone") or (details if details else None) or (_svc_zone_hint if not is_adj else None)

        line_date = _extract_date(line.get("date") or line.get("service_date") or "") or service_date or _extract_date(raw_date)

        nl = {
            "service_type":    line.get("service_type") or (infer_service_type(desc) if not is_adj else desc),
            "description":     desc,
            "date":            line_date,
            "amount":          amount,
            "gt":              line.get("gt") or gt,
            "loa":             line.get("loa") or loa,
            "grt":             line.get("grt") or grt,
            "tug_count":       tug_count,
            "zone":            zone,
            "fx_rate_mxn_usd": line.get("fx_rate_mxn_usd"),
            "is_adjustment":   is_adj,
        }
        normalised_lines.append(nl)

    return {
        "invoice_reference": invoice_reference,
        "vendor":            vendor,
        "vessel_name":       vessel_name,
        "service_date":      service_date,
        "currency":          currency,
        "line_items":        normalised_lines,
    }


def normalise_invoice_fields(inv: dict) -> tuple[str, str, str, str]:
    """Return (invoice_reference, vendor, vessel_name, service_date)."""
    inv = normalise_invoice(inv)
    return (
        inv.get("invoice_reference", ""),
        inv.get("vendor", ""),
        inv.get("vessel_name", ""),
        inv.get("service_date", ""),
    )


def prepare_lines(inv: dict) -> tuple[list, list]:
    """Normalise invoice, inject dimensions, split service / adjustment lines."""
    inv = normalise_invoice(inv)
    service_lines: list = []
    adjustment_lines: list = []
    for line in inv.get("line_items", []):
        if line.get("is_adjustment"):
            adjustment_lines.append(line)
        else:
            service_lines.append(line)
    return service_lines, adjustment_lines
