"""
core/invoice_extractor.py
--------------------------
Converts raw OCR/pdfplumber text into a structured invoice dict
using mistral-small (JSON mode).

This is step 2 of the PDF ingestion pipeline:

  pdf_extractor.extract_text()        →  raw text / markdown
  invoice_extractor.extract_invoice() →  structured dict (descriptions still raw)
  invoice_parser.parse_invoice_line() →  service_type + tug_count + zone_hint per line
  route()                             →  verdict

IMPORTANT: This module does NOT map service_type.
Raw descriptions (e.g. "Atraque buque STI HAMMERSMITH") are left exactly as
found on the invoice. invoice_parser.py + SemanticMatcher handle classification
in the next step.

Output schema
-------------
{
    "invoice_reference": str,
    "vendor":            str,
    "vessel_name":       str,
    "port":              str,
    "service_date":      "YYYY-MM-DD",
    "currency":          str,       # EUR | USD | MXN | DOP | ...
    "line_items": [
        {
            "description":   str,   # raw, untranslated
            "date":          "YYYY-MM-DD",
            "amount":        float,
            "gt":            float | null,
            "loa":           float | null,
            "grt":           float | null,
            "tug_count":     int | null,
            "is_adjustment": bool   # true for surcharges, discounts, VAT lines
        }
    ]
}
"""

import os
import json
import logging
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger(__name__)

MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")

# ---------------------------------------------------------------------------
# EXTRACTION PROMPT
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are a precise invoice data extractor for a ship management company. "
    "You extract structured fields from tugboat / towage invoices. "
    "You always return valid JSON matching the requested schema exactly. "
    "You never translate, normalise, or rephrase raw description text."
)

_USER_PROMPT_TEMPLATE = """\
Extract all fields from the tugboat invoice below and return a single JSON object.

Required schema:
{{
  "invoice_reference": "invoice number — string",
  "vendor":            "towage company name — string",
  "vessel_name":       "ship name — string",
  "port":              "port name — string",
  "service_date":      "YYYY-MM-DD — earliest service date in the invoice",
  "currency":          "EUR | USD | MXN | DOP | etc. — infer from symbols",
  "line_items": [
    {{
      "description":   "raw line description — copy exactly as printed, do not translate",
      "date":          "YYYY-MM-DD",
      "amount":        0.0,
      "gt":            null,
      "loa":           null,
      "grt":           null,
      "tug_count":     null,
      "is_adjustment": false
    }}
  ]
}}

Extraction rules:
- description  : copy the raw text verbatim, including any Spanish/Dutch/German/French words
- is_adjustment: set true for bunker surcharges, fuel adjustments, discounts, VAT, IGIC, IVA lines
- amounts      : always positive floats regardless of sign on invoice (is_adjustment handles type)
- dates        : convert DD/MM/YYYY, DD.MM.YYYY, MMM-DD-YYYY, etc. to ISO YYYY-MM-DD
- gt / loa / grt: look across the whole invoice (header, vessel details, table notes) — null if absent
- tug_count    : extract the integer from the line description if stated, else null
- currency     : € → EUR, $ → USD, MX$ / MXN → MXN, RD$ → DOP, £ → GBP
- service_date : if multiple service dates exist, use the earliest
- If any header field is not found, use null (never use empty string){port_note}

--- INVOICE TEXT ---
{ocr_text}
"""


# ---------------------------------------------------------------------------
# MAIN FUNCTION
# ---------------------------------------------------------------------------

def extract_invoice(
    ocr_text: str,
    port_hint: str = "",
    api_key: Optional[str] = None,
    model: str = "mistral-small-latest",
) -> dict:
    """
    Extract structured invoice fields from raw OCR / pdfplumber text.

    Args:
        ocr_text:  Text returned by pdf_extractor.extract_text()["text"].
        port_hint: Port name if already known (e.g. selected in the UI).
                   Injected into the prompt to help resolve ambiguous names.
        api_key:   Mistral API key. Falls back to MISTRAL_API_KEY env var.
        model:     Mistral chat model. "mistral-small-latest" is the default
                   (cheap, fast, sufficient for structured extraction).

    Returns:
        Structured invoice dict (see module docstring).

    Raises:
        EnvironmentError  — MISTRAL_API_KEY not available.
        ValueError        — Model returned invalid JSON or missing line_items.
        ImportError       — mistralai package not installed.
    """
    try:
        from mistralai import Mistral
    except ImportError:
        raise ImportError(
            "mistralai package is not installed. Run: pip install mistralai"
        )

    key = api_key or MISTRAL_API_KEY
    if not key:
        raise EnvironmentError(
            "MISTRAL_API_KEY is not set. Add it to your .env file."
        )

    port_note = f"\nThe invoice is for port: {port_hint}." if port_hint else ""
    user_message = _USER_PROMPT_TEMPLATE.format(
        port_note=port_note,
        ocr_text=ocr_text,
    )

    client = Mistral(api_key=key)
    logger.info(f"Calling {model} for invoice field extraction...")

    response = client.chat.complete(
        model=model,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user",   "content": user_message},
        ],
    )

    raw = response.choices[0].message.content

    try:
        result = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Mistral returned invalid JSON: {e}\n"
            f"First 500 chars of raw output:\n{raw[:500]}"
        )

    # Minimal validation
    if "line_items" not in result:
        raise ValueError(
            f"Extraction result is missing 'line_items'. "
            f"Keys returned: {list(result.keys())}"
        )

    n_lines = len(result.get("line_items", []))
    n_adj   = sum(1 for li in result.get("line_items", []) if li.get("is_adjustment"))
    logger.info(
        f"Extracted: ref={result.get('invoice_reference')!r}, "
        f"vessel={result.get('vessel_name')!r}, "
        f"lines={n_lines} ({n_lines - n_adj} service + {n_adj} adjustment)"
    )

    return result


# ---------------------------------------------------------------------------
# CONVENIENCE WRAPPER — full PDF → structured dict in one call
# ---------------------------------------------------------------------------

def extract_invoice_from_pdf(
    pdf_path: str,
    port_hint: str = "",
    api_key: Optional[str] = None,
) -> dict:
    """
    Convenience wrapper: PDF file → structured invoice dict.

    Calls pdf_extractor.extract_text() then extract_invoice() in sequence.
    Use this when you have a PDF file path and want the full result in one call.

    Args:
        pdf_path:  Path to the PDF invoice file.
        port_hint: Port name if known (helps extraction accuracy).
        api_key:   Mistral API key.

    Returns:
        Structured invoice dict (see module docstring).
    """
    # Import here to avoid circular dependency if both modules import each other
    from pdf_extractor import extract_text

    extraction = extract_text(pdf_path)
    logger.info(
        f"PDF extraction complete — method: {extraction['method']}, "
        f"pages: {extraction['pages']}, chars: {len(extraction['text'])}"
    )
    return extract_invoice(
        ocr_text=extraction["text"],
        port_hint=port_hint,
        api_key=api_key,
    )


# ---------------------------------------------------------------------------
# CLI — quick test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python core/invoice_extractor.py <path_to_pdf> [port_name]")
        print(f"\nMISTRAL_API_KEY set: {'yes' if MISTRAL_API_KEY else 'NO'}")
    else:
        port = sys.argv[2] if len(sys.argv) > 2 else ""
        try:
            result = extract_invoice_from_pdf(sys.argv[1], port_hint=port)
            print(json.dumps(result, indent=2, ensure_ascii=False))
        except Exception as e:
            print(f"Failed: {e}")
