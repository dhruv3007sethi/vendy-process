"""
core/extraction_prompts.py
--------------------------
Single source of truth for the per-document-type extraction prompts.

These prompts define EXACTLY the JSON shape the rest of the app expects
(the Invoice / SOF / Others paste boxes, and ultimately route()). They are
used in two places:

  1. The Streamlit UI shows them so an officer can copy-paste into ChatGPT.
  2. "Agent 2" (core/document_extractor.py) sends them, together with the
     screenshot rendered by "Agent 1", to the same OpenRouter vision model
     so the JSON is produced automatically.

To change what Agent 2 extracts, edit the prompt text here — both the manual
copy-paste UI and the automated extractor pick up the change.
"""

PROMPT_INVOICE = '''\
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
    "Atraque"                                → Berth
    "Desatraque"                             → Unberth
    "Shift" / "Shifting"                     → Shifting
    "Servicios especiales" / "Special services" → Overtime
- description: include full readable line (tug names, location, berth info).
- date: use the specific event date for that line.
- amount: convert European format — "3.468,83" → 3468.83
- is_adjustment: true ONLY for discounts, VAT, bunker surcharges. false for towage lines
  including Overtime.

4. TUG COUNT AND OPERATIONAL STATUS
Spanish towage invoices mark each tug with an operational status code:
  (C)  = Con maniobra  — tug actively PERFORMED the maneuver
  (SM) = Sin maniobra  — tug was on STANDBY only

Read each tug name in the description and note its suffix:
- A tug with no suffix → treat as (C) active.
- Set fields as follows:
    tug_count        = total tugs on this line (SM + C combined)
    active_tug_count = count of tugs marked (C)
    standby_tug_count= count of tugs marked (SM)

Examples:
  "SERTOSA VEINTISIETE V.B. ALGECIRAS (SM)"
    → SERTOSA VEINTISIETE: no suffix → active (C)
    → V.B. ALGECIRAS (SM): standby
    → tug_count: 2, active_tug_count: 1, standby_tug_count: 1

  "SERTOSA VEINTISIETE V.B. SIMUN (C)"
    → both tugs active (C)
    → tug_count: 2, active_tug_count: 2, standby_tug_count: 0

If no tug status codes appear (e.g. Dutch/Belgian invoices listing "TUG 1 / TUG 2"),
consolidate into ONE line_item with tug_count = number of tugs, active_tug_count = null,
standby_tug_count = null, and amount = TOTAL amount for all tugs combined (sum all tug lines).
Example: TUG 1 = 4,648 and TUG 2 = 4,648 → amount = 9,296, tug_count = 2.

IMPORTANT — Dutch/Belgian adjustment lines (Discounts, Bunker, VAT):
Each TUG block on Dutch/Belgian invoices has its own Discount and Bunker rows beneath it.
You MUST extract these as separate adjustment line_items (is_adjustment: true).
Sum the amounts across all TUG blocks into one line per type.
Example: TUG 1 Discount = 2,184.56 and TUG 2 Discount = 2,184.56 → one adjustment line_item:
  { "description": "Discounts", "amount": 4369.12, "is_adjustment": true }
Example: TUG 1 BUNKER ADJ. FACTOR = 836.64 and TUG 2 BUNKER ADJ. FACTOR = 836.64 → one adjustment line_item:
  { "description": "Bunker Adjustment Factor", "amount": 1673.28, "is_adjustment": true }
Do NOT omit these lines — they are required for the net payable calculation.

5. ZONE
- Extract zone ONLY if an explicit terminal, quay, or berth name is stated on the
  invoice (e.g. "Moeve", "Terminal Norte", "Muelle 7", "Jetty B").
- Do NOT extract tug vessel names as zone. Tug names follow patterns like
  "V.B. ALGECIRAS", "V.B. SIMUN", "SERTOSA VEINTISIETE" — ignore these for zone.
- If no explicit terminal or berth name appears, set zone: null.
- Prefer location tied to the first service (Berth) if multiple appear.

6. GT / LOA EXTRACTION (CRITICAL)

A. Inline vessel format (common on Dutch/Belgian invoices):
   Example: "VESSEL IMO XXXXX GT 64.827 LOA 256,00"

B. Labeled field format (common on Spanish/European invoices):
   Look for: "G.T.", "GT", "Gross Tonnage", "L.O.A.", "LOA", "Length Overall"
   Examples: "G.T./Gross Tonnage: 24.120"  →  gt: 24120
             "LOA: 256,00"                  →  loa: 256.0

C. Normalisation rules (apply to ALL formats):
   European invoices use dot (.) as thousands separator, comma (,) as decimal.
   - Remove dots used as thousands separators
   - Replace commas used as decimal separators with dots
   Examples: "24.120" → 24120  |  "256,00" → 256.0  |  "64.827" → 64827

D. Extraction priority: labeled values → inline vessel string → null

E. Data integrity:
   - NEVER drop leading digits or truncate numbers
   - GT must be a whole number (integer)
   - LOA must be a float if decimals exist

7. CURRENCY
- Always set to "EUR"

8. NOTES (only if present)
- If handwritten or special instructions exist, add:
  "notes": ["text", "text"]

9. STRICT OUTPUT RULES
- Output ONLY JSON — no comments, no explanations
- All numbers properly formatted, all dates in ISO format (YYYY-MM-DD)
'''

PROMPT_SOF = '''\
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
- Key Berth indicators: "first line ashore", "all fast", "tugs made fast" (arrival),
  "commenced mooring", "pilot on board" (inward)
- Key Unberth indicators: "last line", "cast off", "tugs made fast" (departure),
  "commenced unmooring", "passing breakwater" (outward)
- tug_count: extract from the event line if stated (e.g. "2 tugs"), else null
- If the SOF covers multiple vessels, extract events for ALL vessels but note the
  vessel name in the description for each event
- dates: convert DD/MM/YYYY, DD.MM.YYYY to ISO YYYY-MM-DD
- times: convert to 24-hour HH:MM format
'''

PROMPT_TUG_CONFIRMATION = '''\
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
- If tug counts are separate (e.g. "In: 2 / Out: 3"), use each count on its event
- ETD in the subject line = Unberth date. ETA or arrival date = Berth date.
  If only ETD is stated, set Unberth date to that value and Berth date to null.
- Vessel name: extract from the subject line (format: .../VESSEL NAME/.../PORT/...)
- Terminal Name → terminal field; Berth Number → berth field
- Do not invent times or dates not explicitly stated — use null
- If this is an email chain, read the MOST RECENT agent reply for the confirmed
  values (ignore the blank template in the original request)
'''


# ---------------------------------------------------------------------------
# Registry — document_type (as produced by Agent 1) → extraction prompt
# ---------------------------------------------------------------------------
# Agent 1 (document_classifier) emits one of: "invoice", "sof", "other".
# The "other" bucket is treated as a port-agent tug confirmation email, which is
# the only "other" document the engine currently consumes.
EXTRACTION_PROMPTS = {
    "invoice": PROMPT_INVOICE,
    "sof":     PROMPT_SOF,
    "other":   PROMPT_TUG_CONFIRMATION,
}


def prompt_for(document_type: str) -> str:
    """Return the extraction prompt for a document type produced by Agent 1.

    Raises KeyError for an unknown type so callers fail loudly rather than
    silently extracting with the wrong schema.
    """
    key = (document_type or "").strip().lower()
    try:
        return EXTRACTION_PROMPTS[key]
    except KeyError:
        raise KeyError(
            f"No extraction prompt for document_type {document_type!r}. "
            f"Expected one of {tuple(EXTRACTION_PROMPTS)}."
        )
