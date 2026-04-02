"""
run_test_semantic.py
Integration test for SemanticMatcher + invoice_parser pipeline.
Tests multilingual invoice descriptions across 6 ports.

Run:
    cd C:/Scorpio/vendy-process
    python run_test_semantic.py
"""

import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))
sys.path.insert(0, str(pathlib.Path(__file__).parent / "core"))

import logging
logging.basicConfig(level=logging.WARNING)  # Suppress INFO noise during test

from core.semantic_matcher import SemanticMatcher
from core.invoice_parser import parse_invoice_line

print("Loading SemanticMatcher (first run may take a few seconds)...")
matcher = SemanticMatcher()
print("Model ready.\n")

# ── Integration test cases ──────────────────────────────────────────
# Format: (port, raw_description, expected_service_type)
test_cases = [
    ("Tampico",     "Servicio de Atraque TMP-Tampico-Madero 4 - 1.75 horas",   "Berth"),
    ("Manzanillo",  "Maniobra de entrada SILVER VALERIE MAN5180044603",          "Berth"),
    ("Guaymas",     "Remolque de salida STI MARVEL Puerto de Guaymas",           "Unberth"),
    ("Le Havre",    "Remorquage départ Le Havre port",                           "Unberth"),
    ("Antwerp",     "Boegsierdienst aankomst haven Antwerpen",                   "Berth"),
    ("Rostock",     "Schleppdienst Einfahrt Rostock",                            "Berth"),
]

# ── Additional self-check cases ─────────────────────────────────────
extra_cases = [
    ("Algeciras",   "Atraque zona industrial Algeciras",                         "Berth"),
    ("Algeciras",   "Desatraque buque Algeciras",                                "Unberth"),
    ("Rotterdam",   "Boegsierdienst vertrek Rotterdam",                          "Unberth"),
    ("Ceuta",       "Desplazamiento remolcador Ceuta",                           "Displacement"),
    ("Brake",       "Schleppdienst Ausfahrt Brake",                              "Unberth"),
    ("Las Palmas",  "Maniobra salida Las Palmas Gran Canaria",                   "Unberth"),
]

all_cases = test_cases + extra_cases

print("=" * 70)
print("INVOICE PARSER — INTEGRATION TEST")
print("=" * 70)

passed = 0
failed = 0
low_conf = 0

for port, description, expected in all_cases:
    result = parse_invoice_line(description, port, matcher)
    svc    = result["service_type"]
    conf   = result["confidence"]
    review = result["needs_review"]

    ok = svc == expected and conf >= 0.75
    if ok:
        status = "PASS"
        passed += 1
    else:
        status = "FAIL"
        failed += 1
    if conf < 0.75:
        low_conf += 1

    desc_short = description[:55].ljust(55)
    conf_flag  = " LOW" if conf < 0.75 else ""
    mismatch   = f" (got {svc})" if svc != expected else ""
    print(f"  {status} [{port:<22}] {desc_short}  {conf:.2f}{conf_flag}{mismatch}")

print()
print(f"  Passed: {passed}/{len(all_cases)}  |  Failed: {failed}  |  Low confidence: {low_conf}")
print()

if failed == 0:
    print("  ALL TESTS PASSED")
else:
    print(f"  {failed} TEST(S) FAILED — review phrases for failing ports")

# ── Tug hint extraction test ────────────────────────────────────────
from core.semantic_matcher import extract_tug_hint

print()
print("=" * 70)
print("TUG HINT EXTRACTION TEST")
print("=" * 70)

tug_cases = [
    ("2 tugs assist",                  2),
    ("con 3 remolcadores",             3),
    ("met 4 sleepboten Antwerpen",     4),
    ("Schleppdienst 2 Schlepper",      2),
    ("remorquage 3 remorqueurs",       3),
    ("berth operation no tug mention", None),
]

for desc, expected in tug_cases:
    got    = extract_tug_hint(desc)
    status = "PASS" if got == expected else "FAIL"
    print(f"  {status} '{desc}' -> {got!r}  (expected {expected!r})")
