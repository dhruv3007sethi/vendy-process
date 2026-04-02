"""
core/pdf_extractor.py
Extracts text from invoices/SOFs.
- Tries pdfplumber first (text-based PDFs) — free, exact, instant.
- Falls back to Mistral OCR API for image-based / scanned PDFs.

Tesseract is no longer used. Mistral OCR handles multilingual text
automatically so no language code needs to be supplied by the caller.
"""

import os
import base64
import logging
from pathlib import Path
from typing import Dict, Any

import pdfplumber
import fitz  # PyMuPDF — retained for page-count fallback only
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")


# ---------------------------------------------------------------------------
# PRIMARY PATH — pdfplumber (text PDFs)
# ---------------------------------------------------------------------------

def extract_text_pdfplumber(pdf_path: Path) -> str:
    """Extract text from a text-layer PDF using pdfplumber."""
    text = ""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"
    except pdfplumber.PasswordError:
        logger.error(f"PDF is password protected: {pdf_path}")
        raise PermissionError(f"PDF is password protected and cannot be read: {pdf_path}")
    except Exception as e:
        logger.warning(f"pdfplumber encountered an issue (may be image-based): {e}")
        return ""
    return text.strip()


# ---------------------------------------------------------------------------
# FALLBACK PATH — Mistral OCR (scanned / image-based PDFs)
# ---------------------------------------------------------------------------

def extract_text_mistral_ocr(pdf_path: Path, api_key: str) -> str:
    """
    Extract text from a scanned PDF using the Mistral OCR API.

    Sends the PDF as base64 to mistral-ocr-latest.
    Returns concatenated markdown from all pages, preserving table structure.
    Multilingual support is built into the model — no language param needed.
    """
    try:
        from mistralai import Mistral
    except ImportError:
        raise ImportError(
            "mistralai package is not installed. Run: pip install mistralai"
        )

    with open(pdf_path, "rb") as f:
        pdf_b64 = base64.b64encode(f.read()).decode()

    client = Mistral(api_key=api_key)
    logger.info(f"Calling Mistral OCR API for: {pdf_path.name}")

    response = client.ocr.process(
        model="mistral-ocr-latest",
        document={
            "type": "document_url",
            "document_url": f"data:application/pdf;base64,{pdf_b64}",
        },
        table_format="markdown",
    )

    pages = [page.markdown for page in response.pages if page.markdown]
    return "\n\n".join(pages).strip()


# ---------------------------------------------------------------------------
# MAIN ENTRY POINT
# ---------------------------------------------------------------------------

def extract_text(
    pdf_path: str,
    min_text_length: int = 50,
) -> Dict[str, Any]:
    """
    Extract text from any invoice or SOF PDF.

    Strategy:
      1. pdfplumber  — works on all text-layer PDFs, zero cost.
      2. Mistral OCR — fires only when pdfplumber returns < min_text_length
                       chars (i.e. the PDF is scanned / image-based).

    Args:
        pdf_path:        Path to the PDF file.
        min_text_length: Character threshold below which OCR is triggered.
                         Default 50: a real text invoice produces hundreds of
                         chars; a scanned image produces 0-5.

    Returns:
        {
            "text":   str,   # extracted text (markdown if OCR path)
            "method": str,   # "pdfplumber" | "mistral_ocr"
            "pages":  int,   # page count
            "path":   str,   # absolute path to the file
        }

    Raises:
        FileNotFoundError  — PDF does not exist.
        PermissionError    — PDF is password-protected.
        EnvironmentError   — Scanned PDF but MISTRAL_API_KEY not set.
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    # 1. Try pdfplumber
    logger.info(f"Attempting extraction for: {pdf_path.name}")
    try:
        text = extract_text_pdfplumber(pdf_path)
        method = "pdfplumber"
    except PermissionError:
        raise
    except Exception as e:
        logger.warning(f"pdfplumber failed unexpectedly, switching to Mistral OCR. Error: {e}")
        text = ""
        method = "mistral_ocr"

    # 2. Too little text — PDF is image-based, invoke Mistral OCR
    if len(text) < min_text_length:
        api_key = MISTRAL_API_KEY
        if not api_key:
            raise EnvironmentError(
                f"PDF '{pdf_path.name}' appears to be scanned (only {len(text)} chars extracted "
                f"by pdfplumber), but MISTRAL_API_KEY is not set. "
                f"Add MISTRAL_API_KEY=<your_key> to your .env file to enable OCR for scanned PDFs."
            )
        logger.info(f"Text too short ({len(text)} chars) — switching to Mistral OCR...")
        text = extract_text_mistral_ocr(pdf_path, api_key)
        method = "mistral_ocr"

    # 3. Page count (pdfplumber primary, fitz fallback)
    page_count = 0
    try:
        with pdfplumber.open(pdf_path) as pdf:
            page_count = len(pdf.pages)
    except Exception:
        try:
            doc = fitz.open(pdf_path)
            page_count = doc.page_count
            doc.close()
        except Exception as e:
            logger.error(f"Failed to count pages with both libraries: {e}")

    return {
        "text":   text,
        "method": method,
        "pages":  page_count,
        "path":   str(pdf_path),
    }


# ---------------------------------------------------------------------------
# CLI — quick test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python core/pdf_extractor.py <path_to_pdf>")
        print(f"\nMISTRAL_API_KEY set: {'yes' if MISTRAL_API_KEY else 'NO — scanned PDFs will fail'}")
    else:
        try:
            result = extract_text(sys.argv[1])
            print(f"Method : {result['method']}")
            print(f"Pages  : {result['pages']}")
            print(f"Length : {len(result['text'])} chars")
            print(f"\n--- First 500 chars ---\n{result['text'][:500]}")
        except Exception as e:
            print(f"Failed: {e}")
