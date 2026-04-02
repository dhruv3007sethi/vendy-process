"""
core/invoice_parser.py
Parses raw invoice line descriptions into structured service-type inputs
for the route() engine, using SemanticMatcher for multilingual support.

Returns: service_type, tug_count, zone_hint, confidence, matched_term, needs_review

Usage:
    from core.semantic_matcher import SemanticMatcher
    from core.invoice_parser import parse_invoice_line

    matcher = SemanticMatcher()
    result = parse_invoice_line("Servicio de Atraque con 3 remolcadores zona comercial", "Tampico", matcher)
    # {
    #   "service_type": "Berth",
    #   "tug_count":    3,          # extracted from text if present, else None
    #   "zone_hint":    "comercial", # extracted from text if present, else None
    #   "confidence":   0.92,
    #   "matched_term": "Servicio de Atraque Tampico",
    #   "needs_review": False
    # }
"""

import re
from typing import Optional

try:
    from core.semantic_matcher import SemanticMatcher, extract_tug_hint, MIN_CONFIDENCE
except ImportError:
    from semantic_matcher import SemanticMatcher, extract_tug_hint, MIN_CONFIDENCE


# ─────────────────────────────────────────────────────────────────────
# ZONE EXTRACTION
# ─────────────────────────────────────────────────────────────────────

# Common zone indicator patterns found in Spanish/multilingual invoice descriptions.
# Ordered from most-specific to most-generic.
_ZONE_PATTERNS = [
    # Explicit "zona X" pattern (Spanish: "zona comercial", "zona industrial", "zona norte")
    (r"\bzona\s+(\w+)\b",                   lambda m: m.group(1).lower()),

    # "terminal X" (English/Spanish: "terminal a", "terminal norte")
    (r"\bterminal\s+([A-Za-z]\w*)\b",       lambda m: "terminal_" + m.group(1).lower()),

    # "muelle X" — berth/wharf identifier (Spanish)
    (r"\bmuelle\s+(\w+)\b",                 lambda m: "muelle_" + m.group(1).lower()),

    # Compass/direction zones (norte, sur, este, oeste, north, south, east, west)
    (r"\b(norte|sur|este|oeste|north|south|east|west)\b",
                                             lambda m: m.group(1).lower()),

    # Named Dutch/Belgian zones (Zandvliet, Kallo, Antwerp city)
    (r"\b(zandvliet|kallo)\b",              lambda m: m.group(1).lower()),

    # French: "quai X"
    (r"\bquai\s+(\w+)\b",                   lambda m: "quai_" + m.group(1).lower()),

    # German: "Liegeplatz X" (berth identifier)
    (r"\bliegeplatz\s+(\w+)\b",             lambda m: "liegeplatz_" + m.group(1).lower()),

    # Generic: "comercial", "industrial", "general" as standalone words
    (r"\b(comercial|industrial|general)\b", lambda m: m.group(1).lower()),
]


def extract_zone_hint(description: str, zone_map: Optional[dict] = None) -> Optional[str]:
    """
    Extract a zone hint from a raw invoice line description.

    Args:
        description : Raw invoice text.
        zone_map    : Optional port-specific dict mapping exact strings to zone keys.
                      Checked first (case-insensitive substring). Example:
                      {"South Jetty": "south_jetty", "Industrial Zone": "industrial"}

    Returns:
        Zone key string (lowercased) if found, else None.
    """
    # 1. Port-specific exact map (highest confidence)
    if zone_map:
        desc_lower = description.lower()
        for phrase, zone_key in zone_map.items():
            if phrase.lower() in desc_lower:
                return zone_key

    # 2. Pattern-based extraction
    for pattern, extractor in _ZONE_PATTERNS:
        m = re.search(pattern, description, re.IGNORECASE)
        if m:
            return extractor(m)

    return None


# ─────────────────────────────────────────────────────────────────────
# MAIN PARSER
# ─────────────────────────────────────────────────────────────────────

def parse_invoice_line(
    raw_description: str,
    port: str,
    matcher: SemanticMatcher,
    fallback_map: Optional[dict] = None,
    zone_map: Optional[dict] = None,
) -> dict:
    """
    Map a raw invoice line description to structured engine inputs.

    Args:
        raw_description : Free-text line from invoice (any language).
        port            : Port name — passed to SemanticMatcher for filtering.
                          Case-insensitive; underscores treated as spaces.
        matcher         : Shared SemanticMatcher instance (re-use to avoid reload).
        fallback_map    : Optional exact-match dict {phrase: service_type}.
                          Matched case-insensitively via substring check.
                          Bypasses embedding search with 1.0 confidence.
                          Example: {"Atraque": "Berth", "Desatraque": "Unberth"}
        zone_map        : Optional port-specific dict {phrase: zone_key}.
                          Example: {"zona comercial": "comercial", "Sur": "south_docks"}

    Returns:
        {
            "service_type": "Berth",       # mapped service type (Title Case)
            "tug_count":    2,             # tug count extracted from text, or None
            "zone_hint":    "comercial",   # zone key extracted from text, or None
            "confidence":   0.87,          # 0.0–1.0
            "matched_term": "...",         # snippet of matched phrase
            "needs_review": False          # True if confidence < MIN_CONFIDENCE
        }
    """
    tug_count  = extract_tug_hint(raw_description)
    zone_hint  = extract_zone_hint(raw_description, zone_map)

    # 1. Try exact fallback first — 100% reliable for well-known descriptions
    if fallback_map:
        for phrase, svc_type in fallback_map.items():
            if phrase.lower() in raw_description.lower():
                return {
                    "service_type": svc_type,
                    "tug_count":    tug_count,
                    "zone_hint":    zone_hint,
                    "confidence":   1.0,
                    "matched_term": phrase,
                    "needs_review": False,
                }

    # 2. Semantic match via ChromaDB embeddings
    result = matcher.match(raw_description, port=port)

    return {
        "service_type": result["service_type"],
        "tug_count":    tug_count,
        "zone_hint":    zone_hint,
        "confidence":   result["confidence"],
        "matched_term": result.get("matched_term", ""),
        "needs_review": not result["matched"],
    }
