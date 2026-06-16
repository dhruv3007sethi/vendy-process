"""
core/document_classifier.py
---------------------------
"Agent 1" of the ingestion pipeline.

Given an uploaded PDF, this module:
  1. Renders the first page to a PNG image  (render_pdf_first_page).
  2. Sends that image to a vision LLM via OpenRouter and asks it to classify
     the document as one of: "invoice", "sof", "other"  (classify_image).

The convenience wrapper classify_pdf() does both in one call and returns:

    {
        "document_type": "invoice" | "sof" | "other",
        "confidence":    0.0 - 1.0,
        "reason":        str,
        "image_png":     bytes,   # the rendered first-page PNG (for display)
        "model":         str,     # model id actually used
    }

Configuration (read from environment / .env):
    OPENROUTER_API_KEY   required — your OpenRouter key
    OPENROUTER_MODEL     optional — vision-capable chat model id.
                         Must accept image input AND return chat text.
                         (A plain text model or a *rerank* model will NOT work.)

No third-party HTTP dependency is used — only the standard library — so the
module imports cleanly even in minimal environments.
"""

import os
import io
import json
import base64
import logging
import urllib.request
import urllib.error
from typing import Dict, Any

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
# Default to a vision-capable chat model. Override in .env with OPENROUTER_MODEL.
DEFAULT_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-5-nano")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

VALID_TYPES = ("invoice", "sof", "other")

_CLASSIFY_PROMPT = """\
You are a maritime document triage assistant. You are shown an image of the first
page of a scanned document belonging to a tugboat / towage operation.

Classify the document into EXACTLY ONE of these categories:

- "invoice": a bill or invoice from a towage/port company. Signals: "Invoice",
  "Factura", "Invoice No.", line-item charges with amounts, VAT/IVA/IGIC, a
  payable total, vendor billing/header block.
- "sof": a Statement of Facts, timesheet, or port-log of events. Signals:
  "Statement of Facts", chronological event timestamps (EOSP, NOR tendered,
  all fast, first line ashore, pilot on board), times of operations, no money totals.
- "other": anything that is neither a clear invoice nor a clear SOF
  (e.g. a tug confirmation email, cover letter, certificate, blank page).

Respond with ONLY a JSON object, no extra prose:
{"document_type": "invoice" | "sof" | "other", "confidence": <0.0-1.0>, "reason": "<short reason>"}
"""


# ---------------------------------------------------------------------------
# STEP 1 — render PDF first page to PNG
# ---------------------------------------------------------------------------

def render_pdf_first_page(pdf_bytes: bytes, dpi: int = 150) -> bytes:
    """
    Render the first page of a PDF (given as raw bytes) to a PNG image.

    Args:
        pdf_bytes: Raw bytes of the PDF file.
        dpi:       Render resolution. 150 is plenty for classification and
                   keeps the payload small.

    Returns:
        PNG image as bytes.

    Raises:
        ImportError — PyMuPDF (fitz) not installed.
        ValueError  — PDF has no pages or could not be opened.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        raise ImportError(
            "PyMuPDF is not installed. Run: pip install pymupdf"
        )

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        raise ValueError(f"Could not open PDF: {e}")

    if doc.page_count < 1:
        doc.close()
        raise ValueError("PDF contains no pages.")

    page = doc[0]
    pix = page.get_pixmap(dpi=dpi)
    png = pix.tobytes("png")
    doc.close()
    return png


# ---------------------------------------------------------------------------
# STEP 2 — classify the rendered image with a vision LLM (OpenRouter)
# ---------------------------------------------------------------------------

def call_vision_model(
    prompt: str,
    image_png: bytes,
    api_key: str = None,
    model: str = None,
    timeout: int = 60,
) -> str:
    """
    Send one text prompt + one PNG image to an OpenRouter vision model and
    return the model's raw text reply.

    This is the shared low-level call used by both Agent 1 (classification,
    classify_image) and Agent 2 (field extraction, document_extractor) so they
    always hit the SAME model and request shape.

    Args:
        prompt:    The instruction text shown to the model.
        image_png: PNG image bytes (e.g. from render_pdf_first_page()).
        api_key:   OpenRouter API key. Falls back to OPENROUTER_API_KEY env var.
        model:     Vision-capable chat model id. Falls back to OPENROUTER_MODEL
                   env var, then to DEFAULT_MODEL.
        timeout:   HTTP timeout in seconds.

    Returns:
        The raw string content of the model's first choice message.

    Raises:
        EnvironmentError — OPENROUTER_API_KEY not set.
        RuntimeError     — HTTP error or unparseable response envelope.
    """
    key = api_key or OPENROUTER_API_KEY
    if not key:
        raise EnvironmentError(
            "OPENROUTER_API_KEY is not set. Add it to your .env file."
        )
    model_id = model or DEFAULT_MODEL

    b64 = base64.b64encode(image_png).decode()
    data_url = f"data:image/png;base64,{b64}"

    payload = {
        "model": model_id,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
        "temperature": 0,
        # Ask for JSON; harmless if the model ignores it (we also parse defensively).
        "response_format": {"type": "json_object"},
    }

    req = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            # Optional attribution headers recommended by OpenRouter.
            "HTTP-Referer": "https://github.com/scorpio/vendy-process",
            "X-Title": "Vendy VI Calculator",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise RuntimeError(
            f"OpenRouter HTTP {e.code} for model '{model_id}': {detail[:500]}"
        )
    except urllib.error.URLError as e:
        raise RuntimeError(f"OpenRouter request failed: {e.reason}")

    try:
        envelope = json.loads(body)
        return envelope["choices"][0]["message"]["content"]
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        raise RuntimeError(
            f"Unexpected OpenRouter response shape: {e}\n{body[:500]}"
        )


def classify_image(
    image_png: bytes,
    api_key: str = None,
    model: str = None,
    timeout: int = 60,
) -> Dict[str, Any]:
    """
    Send a PNG image to an OpenRouter vision model and classify the document.

    Args:
        image_png: PNG image bytes (e.g. from render_pdf_first_page()).
        api_key:   OpenRouter API key. Falls back to OPENROUTER_API_KEY env var.
        model:     Vision-capable chat model id. Falls back to OPENROUTER_MODEL
                   env var, then to DEFAULT_MODEL.
        timeout:   HTTP timeout in seconds.

    Returns:
        {"document_type": str, "confidence": float, "reason": str, "model": str}

    Raises:
        EnvironmentError — OPENROUTER_API_KEY not set.
        RuntimeError     — HTTP error or unparseable model response.
    """
    model_id = model or DEFAULT_MODEL
    logger.info(f"Classifying document image via OpenRouter model: {model_id}")
    content = call_vision_model(
        _CLASSIFY_PROMPT, image_png, api_key=api_key, model=model_id, timeout=timeout
    )
    parsed = _parse_classification(content)
    parsed["model"] = model_id
    return parsed


def _parse_classification(content: str) -> Dict[str, Any]:
    """
    Defensively parse the model's reply into a normalised classification dict.

    Handles: clean JSON, JSON wrapped in markdown fences, or a bare keyword.
    Always returns a dict with a VALID_TYPES document_type ("other" on doubt).
    """
    text = (content or "").strip()

    # Strip ```json ... ``` fences if present.
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()

    doc_type = None
    confidence = None
    reason = ""

    # Try to locate a JSON object inside the text.
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            obj = json.loads(text[start:end + 1])
            doc_type = str(obj.get("document_type", "")).strip().lower()
            confidence = obj.get("confidence")
            reason = str(obj.get("reason", "")).strip()
        except json.JSONDecodeError:
            pass

    # Fallback: keyword sniffing if JSON parse failed.
    if doc_type not in VALID_TYPES:
        low = text.lower()
        if "invoice" in low:
            doc_type = "invoice"
        elif "sof" in low or "statement of facts" in low:
            doc_type = "sof"
        else:
            doc_type = "other"
        reason = reason or "Derived from non-JSON model reply."

    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 0.5

    return {
        "document_type": doc_type,
        "confidence": max(0.0, min(1.0, confidence)),
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# CONVENIENCE WRAPPER — PDF bytes → classification (+ rendered image)
# ---------------------------------------------------------------------------

def classify_pdf(
    pdf_bytes: bytes,
    api_key: str = None,
    model: str = None,
    dpi: int = 150,
) -> Dict[str, Any]:
    """
    Full path: render a PDF's first page and classify it.

    Returns the classify_image() result plus "image_png" (the rendered PNG,
    so the caller can display the screenshot it classified).
    """
    image_png = render_pdf_first_page(pdf_bytes, dpi=dpi)
    result = classify_image(image_png, api_key=api_key, model=model)
    result["image_png"] = image_png
    return result


# ---------------------------------------------------------------------------
# CLI — quick test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python core/document_classifier.py <path_to_pdf>")
        print(f"\nOPENROUTER_API_KEY set: {'yes' if OPENROUTER_API_KEY else 'NO'}")
        print(f"Model: {DEFAULT_MODEL}")
    else:
        with open(sys.argv[1], "rb") as f:
            pdf_bytes = f.read()
        try:
            res = classify_pdf(pdf_bytes)
            print(f"Type      : {res['document_type']}")
            print(f"Confidence: {res['confidence']:.2f}")
            print(f"Reason    : {res['reason']}")
            print(f"Model     : {res['model']}")
            print(f"Image size: {len(res['image_png'])} bytes PNG")
        except Exception as e:
            print(f"Failed: {e}")
