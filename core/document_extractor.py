"""
core/document_extractor.py
--------------------------
"Agent 2" of the ingestion pipeline.

Agent 1 (document_classifier) renders each uploaded PDF's first page to a PNG
"screenshot", classifies it as "invoice" / "sof" / "other", and routes it into
the matching box. Agent 2 takes that SAME screenshot and the document type, and
asks the SAME OpenRouter vision model to extract the structured JSON the engine
consumes.

    Agent 1:  PDF  → screenshot (PNG) + document_type
    Agent 2:  screenshot (PNG) + document_type → structured JSON

The per-type extraction prompts live in core/extraction_prompts.py (single source
of truth, shared with the manual copy-paste UI). The JSON shape produced here is
exactly what the Invoice / SOF / Others paste boxes — and ultimately route() —
expect.

Public API
----------
    extract_from_image(image_png, document_type, api_key=None, model=None)
        -> {
            "document_type": "invoice" | "sof" | "other",
            "data":          dict,   # the extracted JSON object
            "model":         str,    # model id actually used
            "raw":           str,    # raw model reply (for debugging)
        }
"""

import os
import json
import logging
from typing import Dict, Any

from dotenv import load_dotenv

from .document_classifier import call_vision_model, DEFAULT_MODEL
from .extraction_prompts import prompt_for

load_dotenv()

logger = logging.getLogger(__name__)


def _parse_json_reply(content: str) -> Dict[str, Any]:
    """
    Defensively parse a model reply into a JSON object.

    Handles clean JSON, JSON wrapped in ```json ... ``` fences, or JSON with
    surrounding prose. Raises ValueError if no JSON object can be recovered.
    """
    text = (content or "").strip()

    # Strip ```json ... ``` fences if present.
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()

    # Fast path: the whole reply is a JSON object.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Fallback: locate the outermost { ... } and parse that.
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass

    raise ValueError(
        f"Could not parse JSON from model reply. First 500 chars:\n{text[:500]}"
    )


def extract_from_image(
    image_png: bytes,
    document_type: str,
    api_key: str = None,
    model: str = None,
    timeout: int = 90,
) -> Dict[str, Any]:
    """
    Extract structured JSON from a document screenshot using the OpenRouter
    vision model (the same model Agent 1 used to classify it).

    Args:
        image_png:     PNG screenshot bytes (the rendered first page from Agent 1).
        document_type: "invoice" | "sof" | "other" — selects the extraction prompt.
        api_key:       OpenRouter API key. Falls back to OPENROUTER_API_KEY env var.
        model:         Vision-capable chat model id. Falls back to OPENROUTER_MODEL
                       env var, then to the classifier's DEFAULT_MODEL — so Agent 2
                       uses the same model as Agent 1 by default.
        timeout:       HTTP timeout in seconds (extraction is heavier than triage).

    Returns:
        {"document_type": str, "data": dict, "model": str, "raw": str}

    Raises:
        KeyError          — unknown document_type (no extraction prompt).
        EnvironmentError  — OPENROUTER_API_KEY not set.
        RuntimeError      — HTTP error or unparseable response envelope.
        ValueError        — model reply contained no parseable JSON.
    """
    doc_type = (document_type or "").strip().lower()
    prompt = prompt_for(doc_type)          # raises KeyError on unknown type
    model_id = model or DEFAULT_MODEL

    logger.info(
        f"Extracting {doc_type!r} fields from screenshot via OpenRouter model: {model_id}"
    )
    content = call_vision_model(
        prompt, image_png, api_key=api_key, model=model_id, timeout=timeout
    )
    data = _parse_json_reply(content)

    return {
        "document_type": doc_type,
        "data": data,
        "model": model_id,
        "raw": content,
    }


# ---------------------------------------------------------------------------
# CLI — quick test:  python -m core.document_extractor <pdf> <invoice|sof|other>
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    from .document_classifier import render_pdf_first_page, OPENROUTER_API_KEY

    if len(sys.argv) < 3:
        print("Usage: python -m core.document_extractor <path_to_pdf> <invoice|sof|other>")
        print(f"\nOPENROUTER_API_KEY set: {'yes' if OPENROUTER_API_KEY else 'NO'}")
        print(f"Model: {DEFAULT_MODEL}")
    else:
        with open(sys.argv[1], "rb") as f:
            pdf_bytes = f.read()
        try:
            png = render_pdf_first_page(pdf_bytes)
            res = extract_from_image(png, sys.argv[2])
            print(f"Type : {res['document_type']}")
            print(f"Model: {res['model']}")
            print(json.dumps(res["data"], indent=2, ensure_ascii=False))
        except Exception as e:
            print(f"Failed: {e}")
