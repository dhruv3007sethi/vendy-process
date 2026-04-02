"""
core/semantic_matcher.py
Matches invoice service descriptions to tariff rules using ChromaDB + sentence-transformers.

Example:
  Invoice says  : "Servicio de Atraque"
  Tariff has    : "Berth", "Arrival Berthing", "Atraque"
  Matcher finds : best match + confidence score
"""

import logging
import re
from typing import Optional
import chromadb
from chromadb.utils import embedding_functions

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────

MODEL_NAME      = r"C:\Scorpio\models\all-MiniLM-L6-v2"  # Local, fast, ~90MB
COLLECTION_NAME = "tariff_services"
MIN_CONFIDENCE  = 0.75                  # Below this → flag for agent review


# ─────────────────────────────────────────────────────────────────────
# TUG COUNT EXTRACTION
# ─────────────────────────────────────────────────────────────────────

_TUG_PATTERNS = [
    r"(\d+)\s*(?:tugs?|tug\s+assist)",                      # English
    r"(\d+)\s*remolcadores?",                                # Spanish
    r"(\d+)\s*(?:remolques?\s+asistencia)",                  # Spanish variant
    r"met\s+(\d+)\s*sleepboten?",                            # Dutch (met X sleepboten)
    r"(\d+)\s*sleepboten?",                                  # Dutch (X sleepboten)
    r"(\d+)\s*slepers?",                                     # Dutch colloquial
    r"(\d+)\s*schlepper",                                    # German
    r"(\d+)\s*remorqueurs?",                                 # French
]


def _preprocess(description: str) -> str:
    """
    Strip common invoice noise before semantic matching:
    - Vessel names (runs of 2+ consecutive ALL-CAPS words: SILVER VALERIE, STI MARVEL)
    - Reference/job codes (e.g. MAN5180044603, TMP5180044603)
    - Duration expressions (e.g. "1.75 horas", "4 - 1.75")
    - Port-location codes like "TMP-Tampico-Madero 4"
    """
    # Vessel names: 2+ consecutive sequences of 2+ uppercase letters (STI MARVEL, SILVER VALERIE)
    text = re.sub(r'\b[A-Z]{2,}(?:\s+[A-Z]{2,})+\b', '', description)
    # Reference codes: letter prefix + 6+ digits (e.g. MAN5180044603)
    text = re.sub(r'\b[A-Z]{1,4}\d{6,}\b', '', text)
    # Duration: digits optionally with decimal + horas
    text = re.sub(r'\b\d+(?:[.,]\d+)?\s*horas?\b', '', text, flags=re.IGNORECASE)
    # Zone descriptors: "zona X" is a berth location, not the service type
    text = re.sub(r'\bzona\s+\w+\b', '', text, flags=re.IGNORECASE)
    # "buque" (vessel in Spanish) is noise — the service type is what matters
    text = re.sub(r'\bbuque\b', '', text, flags=re.IGNORECASE)
    # Trailing isolated numbers / dashes (berth numbers, tariff codes)
    text = re.sub(r'[\s\-]+\d+\s*$', '', text)
    # Normalize whitespace
    return re.sub(r'\s+', ' ', text).strip()


def extract_tug_hint(description: str) -> Optional[int]:
    """
    Extract tug count hint from a raw invoice description (any language).
    Returns int if found, None otherwise.
    """
    for pattern in _TUG_PATTERNS:
        m = re.search(pattern, description, re.IGNORECASE)
        if m:
            return int(m.group(1))
    return None


# ─────────────────────────────────────────────────────────────────────
# TARIFF SERVICE DEFINITIONS
# Each entry is one natural-language phrase (not a keyword bag).
# Multiple entries per port × service_type — one per phrase variant.
# sentence-transformers works on sentence semantics, not keyword bags.
# ─────────────────────────────────────────────────────────────────────

def _entries(port: str, service_type: str, phrases: list) -> list:
    """Expand a list of phrases into individual TARIFF_SERVICES entries."""
    port_id = port.lower().replace(" ", "_")
    svc_id  = service_type.lower()
    return [
        {
            "id":           f"{port_id}_{svc_id}_{i}",
            "port":         port,
            "service_type": service_type,
            "text":         phrase,
        }
        for i, phrase in enumerate(phrases)
    ]


# ── Shared phrase sets (reused across ports of the same language group) ──

# Spanish Berth/Unberth/Shift phrases are port-agnostic in meaning;
# we include the port name in some entries for specificity.
def _sp_berth(port: str) -> list:
    return _entries(port, "Berth", [
        f"Servicio de Atraque {port}",
        "Servicio de Atraque",
        f"Maniobra de entrada {port}",
        "Maniobra de entrada",
        "Maniobra de atraque",
        "Atraque entrada",
        "Atraque",
        "Atraque berthing arrival",            # bilingual anchor
        "Atraque del buque",
        f"Remolque de entrada {port}",
        "Remolque de entrada",
        f"Inward towage {port}",
        f"Berthing {port}",
        "Arrival towage",
    ])


def _sp_unberth(port: str) -> list:
    return _entries(port, "Unberth", [
        f"Servicio de Desatraque {port}",
        "Servicio de Desatraque",
        f"Maniobra de salida {port}",
        "Maniobra de salida",
        "Maniobra de desatraque",
        "Desatraque salida",
        "Desatraque",
        "Desatraque unberthing departure",     # bilingual anchor
        "Desatraque del buque",
        f"Remolque de salida {port}",
        "Remolque de salida",
        f"Outward towage {port}",
        f"Unberthing {port}",
        "Departure towage",
    ])


def _sp_shift(port: str) -> list:
    return _entries(port, "Shift", [
        f"Enmienda {port}",
        "Enmienda",
        "Maniobra de enmienda",
        "Cambio de atraque",
        f"Shifting {port}",
        "Shifting berth",
        "Vessel shifting",
    ])


def _nl_berth(port: str) -> list:
    return _entries(port, "Berth", [
        f"Boegsierdienst aankomst {port}",
        f"Boegsierdienst aankomst haven {port}",
        "Boegsierdienst aankomst",
        "Boegsierdienst aankomst haven",
        f"Sleepbootdienst aankomst {port}",
        "Boegseren aankomst",
        f"Afmeren {port}",
        f"Inward towage {port}",
        f"Berthing {port}",
        "Arrival towage",
    ])


def _nl_unberth(port: str) -> list:
    return _entries(port, "Unberth", [
        f"Boegsierdienst vertrek {port}",
        f"Boegsierdienst vertrek haven {port}",
        "Boegsierdienst vertrek",
        "Boegsierdienst vertrek haven",
        f"Sleepbootdienst vertrek {port}",
        "Boegseren vertrek",
        f"Afvaren {port}",
        f"Outward towage {port}",
        f"Unberthing {port}",
        "Departure towage",
    ])


def _nl_shift(port: str) -> list:
    return _entries(port, "Shift", [
        f"Verhalen {port}",
        f"Verhalendienst {port}",
        f"Verschuiven {port}",
        f"Shifting {port}",
        "Verhalen haven",
    ])


def _de_berth(port: str) -> list:
    return _entries(port, "Berth", [
        f"Schleppdienst Einfahrt {port}",
        "Schleppdienst Einfahrt",
        f"Einfahrt {port}",
        f"Ankunft Schlepper {port}",
        f"Einlaufen {port}",
        f"Inward towage {port}",
        f"Berthing {port}",
        "Arrival towage",
    ])


def _de_unberth(port: str) -> list:
    return _entries(port, "Unberth", [
        f"Schleppdienst Ausfahrt {port}",
        "Schleppdienst Ausfahrt",
        f"Ausfahrt {port}",
        f"Abfahrt Schlepper {port}",
        f"Auslaufen {port}",
        f"Outward towage {port}",
        f"Unberthing {port}",
        "Departure towage",
    ])


def _de_shift(port: str) -> list:
    return _entries(port, "Shift", [
        f"Verholen {port}",
        f"Verholdienst {port}",
        f"Umschleppen {port}",
        f"Shifting {port}",
        "Verholen",
    ])


# ── Build TARIFF_SERVICES ─────────────────────────────────────────────

TARIFF_SERVICES = (

    # ── Spanish peninsular ports ──────────────────────────────────
    _sp_berth("Algeciras") + _sp_unberth("Algeciras") + _sp_shift("Algeciras") +
    _sp_berth("Valencia")  + _sp_unberth("Valencia")  + _sp_shift("Valencia") +
    _sp_berth("Huelva")    + _sp_unberth("Huelva")    + _sp_shift("Huelva") +
    _sp_berth("Cadiz Bay") + _sp_unberth("Cadiz Bay") + _sp_shift("Cadiz Bay") +
    _sp_berth("Tenerife La Palma") + _sp_unberth("Tenerife La Palma") + _sp_shift("Tenerife La Palma") +
    _sp_berth("Castellon") + _sp_unberth("Castellon") + _sp_shift("Castellon") +
    _sp_berth("Las Palmas") + _sp_unberth("Las Palmas") + _sp_shift("Las Palmas") +

    # Ceuta — includes standard + displacement/outport_surcharge
    _sp_berth("Ceuta") + _sp_unberth("Ceuta") + _sp_shift("Ceuta") +
    _entries("Ceuta", "Displacement", [
        "Desplazamiento remolcador",
        "Desplazamiento Ceuta",
        "Tug displacement fee",
        "Mobilization fee",
        "Remolcador desplazamiento",
        "Tug travel charge",
        "Dead run fee Ceuta",
    ]) +
    _entries("Ceuta", "Outport_Surcharge", [
        "Recargo zona bahia",
        "Suplemento zona exterior",
        "Outport surcharge",
        "Bay area surcharge",
        "Zona bahia exterior",
        "Recargo fondeadero",
    ]) +

    # ── Dutch / Belgian ports ─────────────────────────────────────
    _nl_berth("Antwerp") + _nl_unberth("Antwerp") + _nl_shift("Antwerp") +
    _nl_berth("Rotterdam") + _nl_unberth("Rotterdam") + _nl_shift("Rotterdam") +
    _nl_berth("Ghent") + _nl_unberth("Ghent") + _nl_shift("Ghent") +
    _nl_berth("Dordrecht Moerdijk") + _nl_unberth("Dordrecht Moerdijk") + _nl_shift("Dordrecht Moerdijk") +

    # ── French port ───────────────────────────────────────────────
    _entries("Le Havre", "Berth", [
        "Remorquage arrivee Le Havre",
        "Remorquage arrivee port Le Havre",
        "Remorquage arrivee",
        "Amarrage Le Havre",
        "Entree port Le Havre",
        "Inward towage Le Havre",
        "Berthing Le Havre",
        "Arrival towage Le Havre",
    ]) +
    _entries("Le Havre", "Unberth", [
        "Remorquage depart Le Havre",
        "Remorquage depart port Le Havre",
        "Remorquage depart",
        "Appareillage Le Havre",
        "Sortie port Le Havre",
        "Outward towage Le Havre",
        "Unberthing Le Havre",
        "Departure towage Le Havre",
    ]) +
    _entries("Le Havre", "Shift", [
        "Dehalage Le Havre",
        "Changement de poste Le Havre",
        "Transfert remorquage Le Havre",
        "Shifting Le Havre",
    ]) +
    _entries("Le Havre", "Waiting", [
        "Temps d attente Le Havre",
        "Attente remorqueur Le Havre",
        "Waiting time Le Havre",
        "Standby Le Havre",
        "Delay charge Le Havre",
    ]) +

    # ── German ports ──────────────────────────────────────────────
    _de_berth("Rostock") + _de_unberth("Rostock") + _de_shift("Rostock") +
    _de_berth("Brake")   + _de_unberth("Brake")   + _de_shift("Brake") +

    # ── Mexican ports ─────────────────────────────────────────────
    _sp_berth("Tampico")      + _sp_unberth("Tampico")      + _sp_shift("Tampico") +
    _sp_berth("Manzanillo")   + _sp_unberth("Manzanillo")   + _sp_shift("Manzanillo") +
    _sp_berth("Guaymas")      + _sp_unberth("Guaymas")      + _sp_shift("Guaymas") +
    _sp_berth("Altamira")     + _sp_unberth("Altamira")     + _sp_shift("Altamira") +
    _sp_berth("Ensenada")     + _sp_unberth("Ensenada")     + _sp_shift("Ensenada") +
    _sp_berth("Mazatlan")     + _sp_unberth("Mazatlan")     + _sp_shift("Mazatlan") +
    _sp_berth("Salina Cruz")  + _sp_unberth("Salina Cruz")  + _sp_shift("Salina Cruz") +
    _sp_berth("Coatzacoalcos") + _sp_unberth("Coatzacoalcos") + _sp_shift("Coatzacoalcos") +

    # ── Panama ────────────────────────────────────────────────────
    _entries("Panama", "Berth", [
        "Berthing Panama",
        "Inbound towage Panama",
        "Harbor tug berthing Panama",
        "Arrival tug Panama",
        "Servicio de Atraque Panama",
        "Maniobra de entrada Panama",
    ]) +
    _entries("Panama", "Unberth", [
        "Unberthing Panama",
        "Outbound towage Panama",
        "Harbor tug departure Panama",
        "Departure tug Panama",
        "Servicio de Desatraque Panama",
        "Maniobra de salida Panama",
    ]) +
    _entries("Panama", "Shift", [
        "Shifting Panama",
        "Port movement Panama",
        "Enmienda Panama",
    ]) +

    # ── Santo Domingo Haina ───────────────────────────────────────
    _sp_berth("Santo Domingo Haina") + _sp_unberth("Santo Domingo Haina") + _sp_shift("Santo Domingo Haina")
)


# ─────────────────────────────────────────────────────────────────────
# MATCHER CLASS
# ─────────────────────────────────────────────────────────────────────

class SemanticMatcher:
    """
    Loads tariff service phrase definitions into ChromaDB.
    Matches incoming invoice descriptions to the closest tariff service.
    """

    def __init__(self, persist_directory: str = "./chroma_db"):
        self.persist_directory = persist_directory
        self.ef = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name=MODEL_NAME
        )
        self.client = chromadb.PersistentClient(path=persist_directory)
        self.collection = self._load_collection()

    def _load_collection(self):
        """Load or create the tariff services collection, upserting all entries."""
        collection = self.client.get_or_create_collection(
            name=COLLECTION_NAME,
            embedding_function=self.ef,
            metadata={"hnsw:space": "cosine"},
        )

        # Upsert is idempotent — safe to call on every init.
        logger.info("Upserting %d tariff phrase entries into ChromaDB...", len(TARIFF_SERVICES))
        collection.upsert(
            ids       = [s["id"]   for s in TARIFF_SERVICES],
            documents = [s["text"] for s in TARIFF_SERVICES],
            metadatas = [{"port": s["port"], "service_type": s["service_type"]}
                         for s in TARIFF_SERVICES],
        )
        logger.info("Collection ready with %d entries.", collection.count())
        return collection

    def match(
        self,
        description: str,
        port: Optional[str] = None,
        n_results: int = 3,
    ) -> dict:
        """
        Match an invoice service description to the closest tariff service.

        Args:
            description : Raw text from invoice (e.g. "Servicio de Atraque")
            port        : Optional port filter. Case-insensitive; underscores
                          treated as spaces (so "Le_Havre" == "Le Havre").
            n_results   : Number of candidates to retrieve

        Returns:
            dict with keys: matched, service_type, matched_term, port,
                            confidence, verdict, alternatives
        """
        where = None
        if port:
            normalized_port = port.strip().replace("_", " ").title()
            where = {"port": normalized_port}

        query_text = _preprocess(description)
        if not query_text:
            query_text = description  # fallback if preprocessing strips everything

        results = self.collection.query(
            query_texts = [query_text],
            n_results   = n_results,
            where       = where,
        )

        if not results["ids"][0]:
            return {
                "matched":      False,
                "service_type": None,
                "matched_term": "",
                "port":         port,
                "confidence":   0.0,
                "verdict":      "NO_MATCH",
                "alternatives": [],
            }

        distances = results["distances"][0]
        metadatas = results["metadatas"][0]
        documents = results["documents"][0]

        best_distance = distances[0]
        best_meta     = metadatas[0]
        confidence    = round(1 - best_distance, 4)
        verdict       = "MATCH" if confidence >= MIN_CONFIDENCE else "LOW_CONFIDENCE"

        alternatives = [
            {
                "service_type": metadatas[i]["service_type"],
                "port":         metadatas[i]["port"],
                "confidence":   round(1 - distances[i], 4),
                "text":         documents[i],
            }
            for i in range(1, len(distances))
        ]

        return {
            "matched":      confidence >= MIN_CONFIDENCE,
            "service_type": best_meta["service_type"],
            "matched_term": documents[0][:80],
            "port":         best_meta["port"],
            "confidence":   confidence,
            "verdict":      verdict,
            "alternatives": alternatives,
        }

    def reset(self):
        """Clear and reload the collection (use when tariff data changes)."""
        self.client.delete_collection(COLLECTION_NAME)
        self.collection = self._load_collection()
        logger.info("Collection reset and reloaded.")


# ─────────────────────────────────────────────────────────────────────
# SELF TEST
# ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    matcher = SemanticMatcher()

    test_cases = [
        ("Servicio de Atraque",              "guaymas"),
        ("Servicio de Desatraque",           "Guaymas"),
        ("Berthing operation",               "Guaymas"),   # English fallback
        ("Unberthing operation",             "Guaymas"),
        ("Displacement fee Ceuta",           "Ceuta"),
        ("Outport surcharge bay area",       "Ceuta"),
        ("Atraque zona industrial",          "Algeciras"),
        ("Desatraque",                       "Algeciras"),
        ("Shifting between berths",          "Algeciras"),
        ("Port towage arrival service",      None),        # No port filter
    ]

    print("\n" + "=" * 65)
    print("SEMANTIC MATCHER -- TEST RESULTS")
    print("=" * 65)

    for description, port in test_cases:
        result = matcher.match(description, port=port)
        status = "PASS" if result["matched"] else "WARN"
        print(f"\n  Input : '{description}' (port={port})")
        print(f"  Match : {result['service_type']} | Confidence: {result['confidence']} [{status}]")
        if result["alternatives"]:
            alt = result["alternatives"][0]
            print(f"  Alt   : {alt['service_type']} ({alt['confidence']})")
