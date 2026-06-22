"""
core/pdf_to_json.py
-------------------
Full PDF ingestion pipeline: classify then extract JSON in a single flow.

Extends core/document_classifier.py (which only classifies) to also
extract a structured invoice or SOF dict from the same vision LLM,
using the same OpenRouter API key — no additional secrets required.

Main entry point:
    classify_and_extract(pdf_bytes, api_key, model)

Returns:
    {
        "document_type":   "invoice" | "sof" | "other",
        "confidence":      float,
        "reason":          str,
        "model":           str,
        "image_png":       bytes,         # first-page PNG for display
        "extracted_json":  dict | None,   # structured invoice / SOF dict
        "extraction_error": str | None,   # set only when extraction fails
    }

Where the prompts live
----------------------
INVOICE_EXTRACTION_PROMPT and SOF_EXTRACTION_PROMPT are the canonical
extraction prompts. app.py imports them from here so they are defined
in exactly one place and reused by both the UI's manual-copy flow and
the automated pipeline.
"""

import io
import json
import base64
import logging
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


# ---------------------------------------------------------------------------
# EXTRACTION PROMPTS  (single source of truth — imported by app.py too)
# ---------------------------------------------------------------------------

INVOICE_EXTRACTION_PROMPT = '''\
You are an expert maritime invoice parser.

Your task is to extract structured data from a towage invoice (which may be scanned,
semi-structured, or contain OCR artifacts).

Output ONLY valid JSON using EXACTLY the structure below.
Do not add explanations or extra text.

Use ISO dates (YYYY-MM-DD). Use null for any field not found.

{
  "invoice_reference": "<invoice number>",
  "vendor": "<issuing company name>",
  "vessel_name": "<vessel name>",
  "service_date": "<date of first towage service on this invoice, YYYY-MM-DD>",
  "currency": "EUR",
  "loa": <vessel length overall in metres, number or null>,
  "gt": <vessel gross tonnage, number or null>,
  "zone": "<explicit terminal, quay or berth name if stated, or null>",
  "line_items": [
    {
      "service_type": "<Berth | Unberth | Shifting>",
      "description": "<full line description including tug names>",
      "date": "<YYYY-MM-DD>",
      "amount": <charge amount as positive number>,
      "tug_count": <total tugs on this line (SM + C combined), or null>,
      "active_tug_count": <tugs marked (C) — performed the maneuver, or null>,
      "standby_tug_count": <tugs marked (SM) — on standby, or null>,
      "is_adjustment": false
    }
  ]
}

--- EXTRACTION RULES ---

1. INVOICE METADATA
- invoice_reference: extract from "Invoice No.", "Factura Nº", or similar.
  Do NOT use PO/Ref/Sales Order/Job Card numbers.
- vendor: extract issuing company from header/footer (NOT the customer block).
- vessel_name: extract from "Vessel", "Buque", or similar field.

2. SERVICE DATE
- service_date = date the first towage service on this invoice occurred.
  Derive it from the date of the first line_item — do NOT use the invoice issue date
  and do NOT scan for earlier non-towage dates (EOSP, NOR, anchorage, etc.).
  Example: if Berth line is dated 2026-02-11 and invoice is dated 2026-02-16,
  set service_date: "2026-02-11".
  On Boluda Rotterdam invoices the service date is on the Sales Order line:
  "Sales Order XXXXX  Job Card XXXXXXX  Date DD.MM.YYYY".

3. LINE ITEMS
- Each charge must be a separate line_item.
- Map service types:
    "Atraque"                                -> Berth
    "Desatraque"                             -> Unberth
    "Shift" / "Shifting" / "Dehalage"        -> Shifting
    "Servicios especiales" / "Special services" -> Overtime
- description: include full readable line (tug names, location, berth info).
- date: use the specific event date for that line.
- amount: convert European format — "3.468,83" -> 3468.83
- is_adjustment: true ONLY for discounts, VAT, bunker surcharges, and holiday/weekend/public
  holiday surcharges billed as separate line items. false for towage lines including Overtime.

4. TUG COUNT AND OPERATIONAL STATUS
Spanish towage invoices mark each tug with an operational status code:
  (C)  = Con maniobra  — tug actively PERFORMED the maneuver
  (SM) = Sin maniobra  — tug was on STANDBY only

Read each tug name in the description and note its suffix:
- A tug with no suffix -> treat as (C) active.
- Set fields as follows:
    tug_count        = total tugs on this line (SM + C combined)
    active_tug_count = count of tugs marked (C)
    standby_tug_count= count of tugs marked (SM)

Examples:
  "SERTOSA VEINTISIETE V.B. ALGECIRAS (SM)"
    -> SERTOSA VEINTISIETE: no suffix -> active (C)
    -> V.B. ALGECIRAS (SM): standby
    -> tug_count: 2, active_tug_count: 1, standby_tug_count: 1

If no tug status codes appear (e.g. Dutch/Belgian invoices listing "TUG 1 / TUG 2"),
consolidate into ONE line_item with tug_count = number of tugs, active_tug_count = null,
standby_tug_count = null, and amount = TOTAL amount for all tugs combined (sum all tug lines).
Example: TUG 1 = 4,648 and TUG 2 = 4,648 -> amount = 9,296, tug_count = 2.

IMPORTANT — Dutch/Belgian adjustment lines (Discounts, Bunker, VAT):
Each TUG block on Dutch/Belgian invoices has its own Discount and Bunker rows beneath it.
You MUST extract these as separate adjustment line_items (is_adjustment: true).
Sum the amounts across all TUG blocks into one line per type.
Example: TUG 1 Discount = 2,184.56 and TUG 2 Discount = 2,184.56 -> one adjustment line_item:
  { "description": "Discounts", "amount": 4369.12, "is_adjustment": true }
Do NOT omit these lines — they are required for the net payable calculation.

5. ZONE
- Extract zone ONLY if an explicit terminal, quay, or berth name is stated on the
  invoice (e.g. "Moeve", "Terminal Norte", "Muelle 7", "Jetty B").
- Do NOT extract tug vessel names as zone.
- If no explicit terminal or berth name appears, set zone: null.

6. GT / LOA EXTRACTION (CRITICAL)

A. Inline vessel format (common on Dutch/Belgian invoices):
   Example: "VESSEL IMO XXXXX GT 64.827 LOA 256,00"

B. Labeled field format (common on Spanish/European invoices):
   Look for: "G.T.", "GT", "Gross Tonnage", "L.O.A.", "LOA", "Length Overall"
   Examples: "G.T./Gross Tonnage: 24.120"  ->  gt: 24120
             "LOA: 256,00"                  ->  loa: 256.0

C. Normalisation rules:
   European invoices use dot (.) as thousands separator, comma (,) as decimal.
   - Remove dots used as thousands separators
   - Replace commas used as decimal separators with dots
   Examples: "24.120" -> 24120  |  "256,00" -> 256.0

7. CURRENCY — always set to "EUR"

8. STRICT OUTPUT RULES
- Output ONLY JSON — no comments, no explanations
- All numbers properly formatted, all dates in ISO format (YYYY-MM-DD)
'''

SOF_EXTRACTION_PROMPT = '''\
Convert this Statement of Facts (SOF) or timesheet to JSON using EXACTLY this structure.
Use ISO dates (YYYY-MM-DD) and 24-hour times (HH:MM). Use null for any field not found.

{
  "vessel": {
    "name": "<vessel name>",
    "imo": "<IMO number as string, or null>",
    "gt": <gross tonnage as number, or null>,
    "loa": <length overall in metres as number, or null>
  },
  "port": "<port name>",
  "events": [
    {
      "service_type": "<Berth | Unberth | Shifting | empty string for non-towage events>",
      "description": "<event description, copy verbatim>",
      "date": "<YYYY-MM-DD>",
      "time": "<HH:MM or null>",
      "tug_count": <number or null>
    }
  ]
}

Rules:
- Include ALL events from the SOF, not just towage events. Non-towage events
  (e.g. EOSP, NOR tendered, commenced loading) get service_type: ""
- Towage events: set service_type to "Berth" (arrival/inward), "Unberth"
  (departure/outward), or "Shifting" (moving between berths)
- Key Berth indicators: "first line ashore", "all fast", "tugs made fast" (arrival)
- Key Unberth indicators: "last line", "cast off", "tugs made fast" (departure)
- tug_count: extract from the event line if stated (e.g. "2 tugs"), else null
- dates: convert DD/MM/YYYY, DD.MM.YYYY to ISO YYYY-MM-DD
- times: convert to 24-hour HH:MM format
- Output ONLY JSON — no comments, no explanations
'''

TUG_CONFIRMATION_EXTRACTION_PROMPT = '''\
Convert this port agent tug confirmation email to JSON using EXACTLY this structure.
Use ISO dates (YYYY-MM-DD) and 24-hour times (HH:MM). Use null for any field not found.

{
  "vessel": {
    "name": "<vessel name — from subject line or email body>",
    "imo": null,
    "gt": null,
    "loa": null
  },
  "port": "<port name>",
  "source_type": "tug_confirmation_email",
  "events": [
    {
      "service_type": "<Berth | Unberth>",
      "description": "<brief description>",
      "date": "<YYYY-MM-DD or null if not stated>",
      "time": "<HH:MM or null if not stated>",
      "tug_count": <number or null>,
      "terminal": "<terminal name or null>",
      "berth": "<berth number/letter or null>"
    }
  ]
}

Rules:
- If "Tug Count (In/Out): N" is a single number, create TWO events — one Berth and
  one Unberth — each with tug_count: N
- ETD in the subject line = Unberth date. ETA or arrival date = Berth date.
- Vessel name: extract from the subject line (format: .../VESSEL NAME/.../PORT/...)
- Terminal Name -> terminal field; Berth Number -> berth field
- Do not invent times or dates not explicitly stated — use null
- Output ONLY JSON — no comments, no explanations
'''

_PROMPT_BY_DOCTYPE = {
    "invoice": INVOICE_EXTRACTION_PROMPT,
    "sof":     SOF_EXTRACTION_PROMPT,
}


# ---------------------------------------------------------------------------
# RENDER ALL PAGES
# ---------------------------------------------------------------------------

def render_pdf_pages(
    pdf_bytes: bytes,
    dpi: int = 100,
    max_pages: int = 6,
) -> List[bytes]:
    """
    Render up to max_pages of a PDF to PNG images.

    Uses a lower DPI than the classifier (100 vs 150) because extraction
    prompts do not need fine visual detail — this keeps the vision LLM
    payload manageable for multi-page documents.
    """
    try:
        import fitz
    except ImportError:
        raise ImportError("PyMuPDF not installed. Run: pip install pymupdf")

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages: List[bytes] = []
    for i in range(min(doc.page_count, max_pages)):
        pix = doc[i].get_pixmap(dpi=dpi)
        pages.append(pix.tobytes("png"))
    doc.close()
    return pages


# ---------------------------------------------------------------------------
# EXTRACT JSON FROM IMAGES VIA VISION LLM
# ---------------------------------------------------------------------------

def extract_json_from_images(
    images: List[bytes],
    prompt: str,
    api_key: str,
    model: str,
    timeout: int = 120,
) -> dict:
    """
    Send one or more page images to the vision LLM with an extraction prompt
    and return the parsed JSON dict.

    Raises:
        RuntimeError — HTTP error or unparseable response.
        ValueError   — Model returned text that is not valid JSON.
    """
    content: List[dict] = [{"type": "text", "text": prompt}]
    for img in images:
        b64 = base64.b64encode(img).decode()
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64}"},
        })

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }

    req = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/scorpio/vendy-process",
            "X-Title": "Vendy VI Calculator",
        },
        method="POST",
    )

    logger.info(f"Extracting JSON from {len(images)} page(s) via {model}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"OpenRouter HTTP {e.code}: {detail[:400]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"OpenRouter request failed: {e.reason}")

    try:
        envelope = json.loads(body)
        raw = envelope["choices"][0]["message"]["content"]
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        raise RuntimeError(f"Unexpected OpenRouter response shape: {e}")

    return _parse_json_response(raw)


def _parse_json_response(content: str) -> dict:
    """
    Defensively parse a JSON object from LLM output.
    Handles markdown fences and leading/trailing prose.
    """
    text = (content or "").strip()

    # Strip ```json ... ``` fences
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()

    # Extract outermost {...}
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError as e:
            raise ValueError(f"Vision model returned invalid JSON: {e}\n{text[:400]}")

    raise ValueError(f"No JSON object found in vision model response:\n{text[:400]}")


# ---------------------------------------------------------------------------
# MAIN ENTRY POINT
# ---------------------------------------------------------------------------

def classify_and_extract(
    pdf_bytes: bytes,
    api_key: str,
    model: Optional[str] = None,
    dpi_classify: int = 150,
    dpi_extract: int = 100,
    max_extract_pages: int = 6,
) -> Dict[str, Any]:
    """
    Full pipeline: render first page → classify → extract JSON.

    Classification uses the first page at dpi_classify (150 default).
    Extraction renders all pages (up to max_extract_pages) at dpi_extract
    (100 default) to keep the vision payload manageable.

    Args:
        pdf_bytes:         Raw bytes of the uploaded PDF.
        api_key:           OpenRouter API key.
        model:             Vision-capable model id. Defaults to the env var
                           OPENROUTER_MODEL, then "openai/gpt-4o-mini".
        dpi_classify:      Render DPI for classification (first page only).
        dpi_extract:       Render DPI for extraction (all pages).
        max_extract_pages: Maximum pages to send to the extraction LLM.

    Returns a dict with keys:
        document_type, confidence, reason, model,
        image_png, extracted_json, extraction_error
    """
    from core.document_classifier import render_pdf_first_page, classify_image

    import os
    resolved_model = model or os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")

    # Step 1: render first page + classify
    first_page = render_pdf_first_page(pdf_bytes, dpi=dpi_classify)
    classification = classify_image(first_page, api_key=api_key, model=resolved_model)

    result: Dict[str, Any] = {
        **classification,
        "model": resolved_model,
        "image_png": first_page,
        "extracted_json": None,
        "extraction_error": None,
    }

    doc_type = classification["document_type"]
    if doc_type not in _PROMPT_BY_DOCTYPE:
        # "other" — no extraction prompt available
        return result

    # Step 2: render all pages + extract JSON
    prompt = _PROMPT_BY_DOCTYPE[doc_type]
    try:
        pages = render_pdf_pages(pdf_bytes, dpi=dpi_extract, max_pages=max_extract_pages)
        extracted = extract_json_from_images(pages, prompt, api_key=api_key, model=resolved_model)
        result["extracted_json"] = extracted
        logger.info(
            f"Extracted {doc_type} JSON: "
            f"{len(extracted.get('line_items', extracted.get('events', [])))} rows"
        )
    except Exception as e:
        result["extraction_error"] = str(e)
        logger.warning(f"JSON extraction failed for {doc_type}: {e}")

    return result
