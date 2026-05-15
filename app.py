import sys
import json
import pathlib
from datetime import datetime, timezone

# Make core/ importable
sys.path.insert(0, str(pathlib.Path(__file__).parent / "core"))

import streamlit as st
from invoice_normaliser import (
    normalise_invoice,
    normalise_invoice_fields,
    normalise_sof,
    prepare_lines,
    is_adjustment_line as _is_adjustment_line,
    infer_service_type as _infer_service_type,
)

ROOT = pathlib.Path(__file__).parent

# ─────────────────────────────────────────────
# Page config
# ─────────────────────────────────────────────
st.set_page_config(
    page_title="VI Calculator",
    page_icon="⚓",
    layout="wide",
)

# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

@st.cache_resource
def load_profiles() -> dict:
    return json.loads((ROOT / "calculation_profiles.json").read_text(encoding="utf-8"))


def load_tariff(port_key: str) -> dict:
    filename = port_key.lower() + ".json"
    path = ROOT / "tariffs" / filename
    if not path.exists():
        raise FileNotFoundError(f"Tariff file not found: tariffs/{filename}")
    return json.loads(path.read_text(encoding="utf-8"))


VERDICT_COLORS = {
    "AUTO_APPROVED": ("#d4edda", "#155724"),   # green bg, green text
    "MISMATCH":      ("#f8d7da", "#721c24"),   # red
    "REVIEW_REQUIRED": ("#fff3cd", "#856404"), # amber
}

LINE_VERDICT_COLORS = {
    "MATCH":       ("#28a745", "white"),
    "MISMATCH":    ("#dc3545", "white"),
    "UNSUPPORTED": ("#6c757d", "white"),
    "REVIEW":      ("#fd7e14", "white"),
}

CONFIDENCE_COLORS = {
    "HIGH":   "#28a745",
    "MEDIUM": "#fd7e14",
    "LOW":    "#dc3545",
}

def var_color(pct: float) -> str:
    """Green ≤1%, amber 1–5%, red >5%."""
    a = abs(pct)
    if a <= 1.0:
        return "#28a745"
    elif a <= 5.0:
        return "#fd7e14"
    else:
        return "#dc3545"

COMMENTS_FILE = ROOT / "officer_comments.json"

# ─────────────────────────────────────────────
# ChatGPT extraction prompts
# ─────────────────────────────────────────────

_PROMPT_INVOICE = '''\
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

_PROMPT_SOF = '''\
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

_PROMPT_TUG_CONFIRMATION = '''\
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

# ─────────────────────────────────────────────
# Premium styling
# ─────────────────────────────────────────────
PREMIUM_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');

html, body, [class*="css"] {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif !important;
}
.stApp { background-color: #f0f3f9; }
.block-container { padding-top: 3.5rem !important; padding-bottom: 3rem !important; }
[data-testid="stHeader"] { background: rgba(240,243,249,0.97); backdrop-filter: blur(4px); }

/* ── Tabs ── */
.stTabs [data-baseweb="tab-list"] {
    background: #0d1f3c; border-radius: 10px 10px 0 0;
    padding: 0.3rem 0.6rem; gap: 0.3rem; border-bottom: none;
}
.stTabs [data-baseweb="tab"] {
    color: #7a9cbf !important; font-weight: 500; font-size: 0.9rem;
    border-radius: 7px; padding: 0.45rem 1.2rem; border: none !important;
    background: transparent !important;
}
.stTabs [aria-selected="true"] {
    background: #c9a84c !important; color: #0d1f3c !important; font-weight: 700 !important;
}
.stTabs [data-baseweb="tab-panel"] { padding-top: 1.5rem; }

/* ── Buttons ── */
button[kind="primary"] {
    background: linear-gradient(135deg, #c9a84c 0%, #ddb94e 100%) !important;
    color: #0d1f3c !important; font-weight: 700 !important; font-size: 0.95rem !important;
    border: none !important; border-radius: 8px !important;
    padding: 0.55rem 2rem !important; letter-spacing: 0.04em !important;
    box-shadow: 0 3px 10px rgba(201,168,76,0.35) !important;
}
button[kind="primary"]:hover {
    box-shadow: 0 5px 18px rgba(201,168,76,0.48) !important; filter: brightness(1.05) !important;
}
button[kind="secondary"] {
    border: 1.5px solid #c0cce5 !important; border-radius: 8px !important;
    color: #0d1f3c !important; font-weight: 600 !important; background: white !important;
}
button[kind="secondary"]:hover { background: #eef2fa !important; border-color: #c9a84c !important; }

/* ── Metrics ── */
[data-testid="stMetric"] {
    background: white; border: 1px solid #dde4f0; border-radius: 10px;
    padding: 1rem 1.2rem !important; box-shadow: 0 1px 6px rgba(13,31,60,0.06);
}
[data-testid="stMetricLabel"] > div {
    color: #6b7a9d !important; font-size: 0.7rem !important; font-weight: 700 !important;
    text-transform: uppercase !important; letter-spacing: 0.1em !important;
}
[data-testid="stMetricValue"] > div {
    color: #0d1f3c !important; font-weight: 700 !important; font-size: 1rem !important;
}

/* ── Expanders (audit trail / paste boxes) ── */
details {
    background: white !important; border: 1px solid #dde4f0 !important;
    border-radius: 9px !important; margin-bottom: 0.5rem !important;
}
details summary {
    color: #1a3a6b !important; font-weight: 500 !important; padding: 0.6rem 0.85rem !important;
}
details[open] summary { border-bottom: 1px solid #edf0f7; }

/* ── File uploader ── */
[data-testid="stFileUploader"] section {
    border: 1.5px dashed #b8c8e0 !important; border-radius: 9px !important;
    background: #f7f9fd !important; transition: border-color 0.2s;
}
[data-testid="stFileUploader"] section:hover { border-color: #c9a84c !important; }

/* ── Inputs / selects / textareas ── */
[data-baseweb="select"] > div { border-radius: 8px !important; border-color: #c0cce5 !important; background: white !important; }
textarea { border-radius: 8px !important; border-color: #c0cce5 !important; background: #f7f9fd !important; font-size: 0.82rem !important; }
input[type="number"] { border-radius: 8px !important; }

/* ── Alerts ── */
[data-testid="stAlert"] { border-radius: 9px !important; border-left-width: 4px !important; }

/* ── Dataframe ── */
[data-testid="stDataFrame"] {
    border-radius: 10px !important; overflow: hidden;
    border: 1px solid #dde4f0 !important; box-shadow: 0 1px 6px rgba(13,31,60,0.05) !important;
}

/* ── Table (adjustment lines) ── */
table th {
    background: #0d1f3c !important; color: #c9a84c !important; font-size: 0.75rem !important;
    font-weight: 700 !important; letter-spacing: 0.08em !important;
    text-transform: uppercase !important; padding: 0.6rem 0.9rem !important;
}
table td {
    background: white !important; color: #1a2540 !important; font-size: 0.88rem !important;
    padding: 0.5rem 0.9rem !important; border-bottom: 1px solid #edf0f7 !important;
}

/* ── Dividers ── */
hr { border-color: #dde4f0 !important; margin: 1.25rem 0 !important; }

/* ── Captions ── */
[data-testid="stCaptionContainer"] p { color: #6b7a9d !important; }

/* ── Download button ── */
.stDownloadButton > button {
    border: 1.5px solid #1a3a6b !important; border-radius: 8px !important;
    color: #1a3a6b !important; font-weight: 600 !important; background: white !important;
}
.stDownloadButton > button:hover { background: #eef2fa !important; }

/* ── Headings (upload box labels h4) ── */
h4 { color: #0d1f3c !important; font-weight: 700 !important; margin-bottom: 0.4rem !important; }
</style>
"""


def _section_header(text: str) -> None:
    """Render a premium styled section header (replaces st.subheader)."""
    st.markdown(
        f'<div style="border-left:3px solid #c9a84c;padding:0.15rem 0 0.15rem 0.85rem;'
        f'margin:1.5rem 0 0.9rem;color:#0d1f3c;font-size:1.05rem;font-weight:700;'
        f'letter-spacing:0.02em;">{text}</div>',
        unsafe_allow_html=True,
    )


def load_comments() -> list:
    if COMMENTS_FILE.exists():
        try:
            return json.loads(COMMENTS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def save_comments(records: list) -> None:
    COMMENTS_FILE.write_text(
        json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def badge(label: str, bg: str, fg: str) -> str:
    return (
        f'<span style="background:{bg};color:{fg};padding:2px 10px;'
        f'border-radius:12px;font-size:0.82em;font-weight:600;">{label}</span>'
    )


def verdict_badge(verdict: str) -> str:
    bg, fg = LINE_VERDICT_COLORS.get(verdict, ("#6c757d", "white"))
    return badge(verdict, bg, fg)


def confidence_chip(label: str) -> str:
    color = CONFIDENCE_COLORS.get(label, "#6c757d")
    return badge(label, color, "white")


def fmt_eur(val: float, currency: str = "EUR") -> str:
    return f"{currency} {val:,.2f}"




# ─────────────────────────────────────────────
# Load static data
# ─────────────────────────────────────────────
profiles_data = load_profiles()
all_port_keys = sorted(profiles_data["calculation_profiles"].keys())

# ─────────────────────────────────────────────
# Inject global styles
# ─────────────────────────────────────────────
st.markdown(PREMIUM_CSS, unsafe_allow_html=True)

# ─────────────────────────────────────────────
# Top-level tabs
# ─────────────────────────────────────────────
tab_verify, tab_log = st.tabs(["⚓ Verification", "📋 Officer Comments Log"])

# ══════════════════════════════════════════════
# TAB 1 — VERIFICATION
# ══════════════════════════════════════════════
with tab_verify:
    st.markdown("""
<div style="
    background: linear-gradient(135deg, #0d1f3c 0%, #1a3a6b 100%);
    padding: 2rem 2.5rem 1.75rem;
    border-radius: 12px;
    margin-top: 2rem;
    margin-bottom: 1.75rem;
    border-left: 4px solid #c9a84c;
    box-shadow: 0 6px 28px rgba(13,31,60,0.18);
">
    <div style="color:#c9a84c;font-size:0.7rem;font-weight:700;letter-spacing:0.22em;text-transform:uppercase;margin-bottom:0.55rem;">
        GET Marine Mgt
    </div>
    <div style="color:white;font-size:2.1rem;font-weight:700;font-style:italic;font-family:Georgia,serif;line-height:1.2;">
        VI Calculator
    </div>
    <div style="color:#8eadd4;font-size:0.87rem;margin-top:0.55rem;font-weight:400;">
        Upload invoice &amp; SOF &nbsp;·&nbsp; Select port &nbsp;·&nbsp; Run
    </div>
</div>
""", unsafe_allow_html=True)

    # ── JSON validation helper ─────────────────
    def _valid_json(s: str) -> bool:
        if not s:
            return False
        try:
            json.loads(s)
            return True
        except Exception:
            return False

    def _json_status(s: str) -> None:
        """Render a small ✅/❌ status line below a paste area."""
        if not s.strip():
            return
        try:
            json.loads(s)
            st.markdown('<small style="color:#28a745">✅ Valid JSON</small>',
                        unsafe_allow_html=True)
        except Exception as e:
            st.markdown(f'<small style="color:#dc3545">❌ Invalid JSON — {e}</small>',
                        unsafe_allow_html=True)

    # ── Upload boxes ──────────────────────────
    col_inv, col_sof, col_oth = st.columns(3)

    with col_inv:
        st.markdown("#### 📄 Invoice")
        invoice_file = st.file_uploader(
            "Upload Invoice JSON",
            type=["json"],
            key="invoice_upload",
            label_visibility="collapsed",
        )
        with st.expander("Or paste JSON directly"):
            st.text_area(
                "Invoice JSON text",
                key="invoice_paste",
                height=120,
                placeholder='{\n  "invoice_reference": "...",\n  "line_items": [...]\n}',
                label_visibility="collapsed",
            )
            _json_status(st.session_state.get("invoice_paste", ""))

    with col_sof:
        st.markdown("#### 📋 SOF")
        sof_file = st.file_uploader(
            "Upload SOF JSON",
            type=["json"],
            key="sof_upload",
            label_visibility="collapsed",
        )
        with st.expander("Or paste JSON directly"):
            st.text_area(
                "SOF JSON text",
                key="sof_paste",
                height=120,
                placeholder='{\n  "vessel": {...},\n  "events": [...]\n}',
                label_visibility="collapsed",
            )
            _json_status(st.session_state.get("sof_paste", ""))

    with col_oth:
        st.markdown("#### 📎 Others")
        other_files = st.file_uploader(
            "Upload other files",
            accept_multiple_files=True,
            key="other_upload",
            label_visibility="collapsed",
        )
        if other_files:
            st.markdown("**Uploaded files:**")
            for f in other_files:
                st.markdown(f"- `{f.name}`")
        with st.expander("Or paste JSON directly"):
            st.text_area(
                "Others JSON text",
                key="others_paste",
                height=120,
                placeholder='{\n  "source_type": "tug_confirmation_email",\n  "vessel": {...},\n  "events": [...]\n}',
                label_visibility="collapsed",
            )
            _json_status(st.session_state.get("others_paste", ""))
        if other_files or st.session_state.get("others_paste", "").strip():
            st.info("Will be auto-processed in a future release.")

    # ── ChatGPT extraction prompts ────────────
    with st.expander("💬 ChatGPT extraction prompts — click to copy & paste into ChatGPT"):
        pt_inv, pt_sof, pt_tug = st.tabs(["📄 Invoice", "📋 SOF", "📎 Tug Confirmation"])
        with pt_inv:
            st.caption("Use this prompt to convert an invoice image or PDF into the Invoice JSON required above.")
            st.code(_PROMPT_INVOICE, language=None)
        with pt_sof:
            st.caption("Use this prompt to convert a Statement of Facts (SOF) or timesheet into the SOF JSON required above.")
            st.code(_PROMPT_SOF, language=None)
        with pt_tug:
            st.caption("Use this prompt when you only have a tug count confirmation email from the port agent (no full SOF).")
            st.code(_PROMPT_TUG_CONFIRMATION, language=None)

    st.divider()

    # ── Port selector ─────────────────────────
    selected_port = st.selectbox("Select Port", options=all_port_keys, index=None,
                                 placeholder="Choose a port…")

    # ── Resolve inputs (file takes precedence over paste) ─────────────
    invoice_text = st.session_state.get("invoice_paste", "").strip()
    sof_text     = st.session_state.get("sof_paste", "").strip()
    invoice_ready = invoice_file is not None or _valid_json(invoice_text)
    sof_ready     = sof_file     is not None or _valid_json(sof_text)

    # ── Run button ────────────────────────────
    can_run = invoice_ready and sof_ready and selected_port is not None
    run_clicked = st.button(
        "▶ Run Verification",
        disabled=not can_run,
        type="primary",
        use_container_width=False,
    )

    if not can_run and not run_clicked:
        missing = []
        if not invoice_ready:
            missing.append("invoice JSON")
        if not sof_ready:
            missing.append("SOF JSON")
        if selected_port is None:
            missing.append("port selection")
        if missing:
            st.caption(f"Waiting for: {', '.join(missing)}")

    # ── Engine call ───────────────────────────
    if run_clicked and can_run:
        try:
            from port_router import route  # imported late to avoid top-level failure

            invoice_dict = json.load(invoice_file) if invoice_file else json.loads(invoice_text)
            sof_dict     = json.load(sof_file)     if sof_file     else json.loads(sof_text)
            tariff_dict  = load_tariff(selected_port)

            sof_dict     = normalise_sof(sof_dict)   # handle ChatGPT SOF format
            invoice_reference, vendor, vessel_name, service_date = normalise_invoice_fields(invoice_dict)
            service_lines, adjustment_lines = prepare_lines(invoice_dict)

            # Cross-propagate vessel dimensions: if the SOF vessel block is missing
            # loa/gt/grt, fill from the invoice header (both describe the same vessel).
            _vessel_block = sof_dict.setdefault("vessel", {})
            for _dim_key, _inv_keys in (
                ("loa", ("loa", "loa_meters")),
                ("gt",  ("gt", "gross_tonnage")),
                ("grt", ("grt",)),
            ):
                if not _vessel_block.get(_dim_key):
                    for _k in _inv_keys:
                        _v = invoice_dict.get(_k)
                        if _v:
                            _vessel_block[_dim_key] = _v
                            break

            with st.spinner("Running engine…"):
                result = route(
                    port=selected_port,
                    sof_data=sof_dict,
                    invoice_lines=service_lines,
                    tariff_data=tariff_dict,
                    calculation_profiles=profiles_data,
                    invoice_reference=invoice_reference,
                    vendor=vendor,
                    vessel_name=vessel_name,
                    service_date=service_date,
                    match_tolerance_pct=1.0,
                    adjustment_lines=adjustment_lines,
                )

            st.session_state["result"]           = result
            st.session_state["adjustment_lines"] = adjustment_lines
            st.session_state["selected_port"]    = selected_port

        except FileNotFoundError as e:
            fname = str(e)
            st.error(
                f"**Tariff file not found for port `{selected_port}`.**\n\n"
                f"{fname}\n\n"
                f"Check that `tariffs/{selected_port.lower()}.json` exists in the project folder."
            )
        except ValueError as e:
            msg = str(e)
            if "not found in calculation profiles" in msg:
                available = sorted(profiles_data["calculation_profiles"].keys())
                st.error(
                    f"**Port `{selected_port}` is not configured.**\n\n"
                    f"Available ports: {', '.join(available)}"
                )
            elif "No GT bracket found" in msg or "No LOA bracket" in msg:
                # Extract dimension info from the error message if present
                st.error(
                    f"**Vessel dimension outside tariff bracket range.**\n\n"
                    f"{msg}\n\n"
                    f"The vessel's GT or LOA falls outside all rows in the `{selected_port}` "
                    f"tariff table. Check the tariff JSON or raise with the port agent."
                )
            else:
                st.error(f"**Calculation error:** {msg}")
        except Exception as e:
            st.error(f"**Unexpected engine error:** {e}")
            raise

    # ── Results ───────────────────────────────
    if "result" in st.session_state:
        result: dict          = st.session_state["result"]
        adjustment_lines: list = st.session_state["adjustment_lines"]
        port_key: str          = st.session_state["selected_port"]

        verdict = result.get("overall_verdict", "")

        # ── 8a. Verdict banner ─────────────────
        bg, fg = VERDICT_COLORS.get(verdict, ("#e9ecef", "#212529"))
        VERDICT_ICONS   = {"AUTO_APPROVED": "✅", "MISMATCH": "⚠️", "REVIEW_REQUIRED": "🔍"}
        VERDICT_LABELS  = {
            "AUTO_APPROVED":   "All charges verified within tolerance",
            "MISMATCH":        "One or more line items exceed the variance threshold",
            "REVIEW_REQUIRED": "Engine flagged items requiring human review",
        }
        VERDICT_BORDERS = {"AUTO_APPROVED": "#155724", "MISMATCH": "#721c24", "REVIEW_REQUIRED": "#856404"}
        v_icon   = VERDICT_ICONS.get(verdict, "")
        v_label  = VERDICT_LABELS.get(verdict, "")
        v_border = VERDICT_BORDERS.get(verdict, "#6c757d")
        cur_v    = result.get("currency", "EUR")
        st.markdown(f"""
<div style="background:{bg};color:{fg};padding:1.4rem 2rem;border-radius:10px;
            margin:1.25rem 0;border-left:5px solid {v_border};
            box-shadow:0 2px 14px rgba(0,0,0,0.08);">
  <div style="font-size:1.55rem;font-weight:800;letter-spacing:0.03em;">
      {v_icon}&nbsp;&nbsp;{verdict}
  </div>
  <div style="font-size:0.87rem;margin-top:0.3rem;opacity:0.85;">{v_label}</div>
  <div style="font-size:0.8rem;margin-top:0.55rem;opacity:0.7;">
      Expected: <strong>{fmt_eur(result.get("total_expected",0), cur_v)}</strong>
      &nbsp;·&nbsp;
      Invoiced: <strong>{fmt_eur(result.get("total_invoiced",0), cur_v)}</strong>
      &nbsp;·&nbsp;
      Variance: <strong>{fmt_eur(result.get("total_variance",0), cur_v)}</strong>
  </div>
</div>
""", unsafe_allow_html=True)

        # ── 8b-pre. Dim value missing warning ──
        _dim_val  = result.get("vessel_dimension_value", None)
        _dim_type = result.get("vessel_dimension_type", "GT")
        if _dim_val is not None and float(_dim_val) == 0.0:
            _dim_key_map = {
                "GT":  "`gt`",
                "GRT": "`grt` (or `trb`)",
                "LOA_meters": "`loa`",
                "LOA": "`loa`",
            }
            _dim_key = _dim_key_map.get(_dim_type, f"`{_dim_type.lower()}`")
            st.warning(
                f"**Vessel dimension missing — calculation used 0.0 {_dim_type}.**\n\n"
                f"The `{selected_port}` tariff uses **{_dim_type}** as the billing dimension, "
                f"but no {_dim_key} value was found in the SOF vessel block or in any invoice line.\n\n"
                f"**What the engine did:** It continued the calculation with {_dim_type} = 0, "
                f"producing a base rate of 0 (or only the fixed component). "
                f"The expected amount shown is therefore incorrect.\n\n"
                f"**To fix:** Add {_dim_key} to the SOF `vessel` block "
                f"or as a top-level field in the invoice JSON (e.g. `\"loa\": 184.0`), "
                f"then re-run verification."
            )

        # ── 8b. Invoice header ──────────────────
        _section_header("Invoice Summary")
        h1, h2, h3, h4 = st.columns(4)
        h1.metric("Vessel",    result.get("vessel_name", "—"))
        h2.metric("Port",      result.get("port", "—"))
        h3.metric("Vendor",    result.get("vendor", "—"))
        h4.metric("Invoice Ref", result.get("invoice_reference", "—"))

        h5, h6, h7, h8 = st.columns(4)
        h5.metric("Service Date",  result.get("service_date", "—"))
        h6.metric("Currency",      result.get("currency", "—"))
        h7.metric("Total Expected", fmt_eur(result.get("total_expected", 0), result.get("currency", "EUR")))
        h8.metric("Total Invoiced", fmt_eur(result.get("total_invoiced", 0), result.get("currency", "EUR")))

        variance_val = result.get("total_variance", 0)
        conf_label   = result.get("overall_confidence_label", "")
        conf_score   = result.get("overall_confidence", 0.0)

        hc1, hc2, hc3 = st.columns(3)
        hc1.metric("Total Variance", fmt_eur(variance_val, result.get("currency", "EUR")))
        hc2.metric("Confidence",     f"{conf_label} ({conf_score:.0%})")
        hc3.metric("Validated At",   result.get("validated_at", "—")[:19].replace("T", " "))

        _inv_gross = result.get("invoice_amount_gross", 0.0)
        _fuel_sc   = result.get("fuel_surcharge_total", 0.0)
        _inv_net   = result.get("invoice_amount_net", 0.0)
        if _inv_gross or _inv_net:
            hn1, hn2, hn3 = st.columns(3)
            hn1.metric("Invoice Amount Gross", fmt_eur(_inv_gross, result.get("currency", "EUR")))
            hn2.metric("Fuel Surcharge",       fmt_eur(_fuel_sc,   result.get("currency", "EUR")))
            hn3.metric("Invoice Amount Net",   fmt_eur(_inv_net,   result.get("currency", "EUR")))

        st.divider()

        # ── 8c–8h. Line items ──────────────────
        _section_header("Line Items")

        line_items = result.get("line_items", [])
        currency   = result.get("currency", "EUR")

        for li in line_items:
            ln        = li.get("line_number", "?")
            desc      = li.get("service_description", "")
            expected  = li.get("expected_amount", 0.0)
            invoiced  = li.get("invoiced_amount", 0.0)
            var_pct   = li.get("variance_pct", 0.0)
            var_amt   = li.get("variance", 0.0)
            lv        = li.get("verdict", "")
            cl        = li.get("confidence_label", "")
            cs        = li.get("confidence_score", 0.0)

            # ── Card header row ───────────────────
            lv_bg, lv_fg = LINE_VERDICT_COLORS.get(lv, ("#6c757d", "white"))
            st.markdown(f"""
<div style="background:white;border:1px solid #dde4f0;border-radius:10px;
            padding:0.9rem 1.25rem 0.75rem;margin-bottom:0.5rem;
            box-shadow:0 1px 5px rgba(13,31,60,0.06);
            border-left:4px solid {lv_bg};">
  <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:0.5rem;">
    <div style="font-weight:700;color:#0d1f3c;font-size:0.97rem;">
        Line {ln} &nbsp;<span style="color:#6b7a9d;font-weight:400;">—</span>&nbsp; {desc}
    </div>
    <span style="background:{lv_bg};color:{lv_fg};padding:3px 14px;border-radius:20px;
                 font-size:0.78rem;font-weight:700;letter-spacing:0.05em;">{lv}</span>
  </div>
  <div style="display:flex;gap:2.5rem;margin-top:0.55rem;flex-wrap:wrap;">
    <div><span style="color:#6b7a9d;font-size:0.72rem;font-weight:600;text-transform:uppercase;letter-spacing:0.08em;">Expected</span><br>
         <span style="color:#0d1f3c;font-weight:600;font-size:0.92rem;">{fmt_eur(expected, currency)}</span></div>
    <div><span style="color:#6b7a9d;font-size:0.72rem;font-weight:600;text-transform:uppercase;letter-spacing:0.08em;">Invoiced</span><br>
         <span style="color:#0d1f3c;font-weight:600;font-size:0.92rem;">{fmt_eur(invoiced, currency)}</span></div>
    <div><span style="color:#6b7a9d;font-size:0.72rem;font-weight:600;text-transform:uppercase;letter-spacing:0.08em;">Variance</span><br>
         <span style="color:{var_color(var_pct)};font-weight:700;font-size:0.92rem;">{var_pct:+.2f}%</span>
         <span style="color:#6b7a9d;font-size:0.78rem;"> ({fmt_eur(var_amt, currency)})</span></div>
  </div>
</div>
""", unsafe_allow_html=True)

            # ── 8d. Audit trail expander (sits below the card) ──────
            with st.expander(f"Line {ln} — audit trail & details"):
                ac1, ac2 = st.columns(2)
                with ac1:
                    st.markdown(f"**SOF event:** {li.get('sof_event_cited') or '—'}")
                    st.markdown(f"**Tariff rule:** {li.get('tariff_rule_cited') or '—'}")
                    st.markdown(f"**Handler used:** `{li.get('handler_used') or '—'}`")
                with ac2:
                    st.markdown(
                        f"**Confidence:** {confidence_chip(cl)} "
                        f"<small>({cs:.0%})</small>",
                        unsafe_allow_html=True,
                    )
                    if li.get("overtime_applied"):
                        st.markdown(f"**Overtime:** {li['overtime_applied']}")
                    if li.get("human_review_flag"):
                        reason = li.get("human_review_reason", "")
                        if "No GT bracket" in reason or "No LOA bracket" in reason or "CALCULATION_ERROR" in li.get("tariff_rule_cited", ""):
                            st.error(
                                f"**Vessel outside tariff bracket range.** "
                                f"The vessel dimension does not match any row in the tariff table. "
                                f"Contact the port agent to confirm the correct tariff applies. "
                                f"Detail: {reason}"
                            )
                        else:
                            st.warning(f"Review flag: {reason}")
                    if li.get("notes"):
                        st.markdown(f"**Notes:** {li['notes']}")

                surcharges = li.get("surcharges_applied") or []
                if surcharges:
                    st.markdown("**Surcharges applied:**")
                    for s in surcharges:
                        st.markdown(
                            f"- {s.get('name')} — ×{s.get('multiplier')} "
                            f"= {fmt_eur(s.get('amount', 0), currency)} "
                            f"({s.get('citation', '')})"
                        )

                # ── 8e. Candidate explanations (per line) ──
                expls = li.get("candidate_explanations") or []
                if expls:
                    st.markdown("**Candidate explanations for variance:**")
                    for ex in expls:
                        st.markdown(
                            f"- *{ex.get('type', '')}* — {ex.get('description', '')} "
                            f"→ expected {fmt_eur(ex.get('expected_amount', 0), currency)} "
                            f"({ex.get('variance_pct', 0):+.2f}%)"
                        )

            # ── 8h. Officer comment inputs ────
            with st.container():
                oc1, oc2 = st.columns([3, 1])
                with oc1:
                    st.text_area(
                        label=f"Officer notes — Line {ln} ({desc})",
                        placeholder=(
                            "e.g. Expected rate was X — vendor applied Y. "
                            "Tariff table row used appears incorrect."
                        ),
                        key=f"comment_line_{ln}",
                        height=80,
                    )
                with oc2:
                    st.number_input(
                        "Officer expected amount (0 = no override)",
                        min_value=0.0,
                        step=0.01,
                        format="%.2f",
                        key=f"override_line_{ln}",
                    )

            st.markdown('<div style="margin-bottom:1.25rem;"></div>', unsafe_allow_html=True)

        # ── 8g. Summary notes ──────────────────
        notes = result.get("summary_notes") or []
        if notes:
            st.info("\n\n".join(notes))

        # ── 8f. Adjustment lines ───────────────
        if adjustment_lines:
            _section_header("Informational — not tariff validated")
            st.caption("Adjustment lines (bunker surcharges, fuel adjustments, discounts) are shown here for completeness only.")
            adj_rows = []
            for a in adjustment_lines:
                adj_rows.append({
                    "Description": a.get("description", ""),
                    "Amount":      fmt_eur(a.get("amount", 0), currency),
                    "Currency":    a.get("currency", currency),
                })
            st.table(adj_rows)

        st.divider()

        # ── 8i. Save comments ──────────────────
        if st.button("💾 Save Officer Comments", key="save_comments_btn"):
            now_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
            existing = load_comments()
            # Build lookup by key for upsert
            lookup: dict[str, int] = {
                r["_key"]: i for i, r in enumerate(existing) if "_key" in r
            }

            saved_count = 0
            for li in line_items:
                ln   = li.get("line_number")
                desc = li.get("service_description", "")
                comment_text = st.session_state.get(f"comment_line_{ln}", "").strip()
                override_val = st.session_state.get(f"override_line_{ln}", 0.0)

                if not comment_text and (not override_val or override_val == 0.0):
                    continue  # nothing to save for this line

                rec_key = f"{port_key}::{result.get('invoice_reference','')}::{result.get('service_date','')}::{ln}"
                record = {
                    "_key":                    rec_key,
                    "port":                    port_key,
                    "invoice_reference":       result.get("invoice_reference", ""),
                    "vessel_name":             result.get("vessel_name", ""),
                    "service_date":            result.get("service_date", ""),
                    "line_number":             ln,
                    "service_description":     desc,
                    "engine_expected":         li.get("expected_amount"),
                    "invoiced_amount":         li.get("invoiced_amount"),
                    "officer_expected_override": override_val if override_val else None,
                    "officer_comment":         comment_text,
                    "engine_verdict":          li.get("verdict", ""),
                    "variance_pct":            li.get("variance_pct"),
                    "handler_used":            li.get("handler_used", ""),
                    "saved_at":                now_utc,
                }

                if rec_key in lookup:
                    existing[lookup[rec_key]] = record
                else:
                    existing.append(record)
                saved_count += 1

            if saved_count:
                try:
                    save_comments(existing)
                    st.success(f"Saved {saved_count} comment(s) to officer_comments.json")
                except Exception as e:
                    st.error(f"Failed to save comments: {e}")
            else:
                st.info("No comments or overrides entered — nothing to save.")

        st.divider()

        # ── 8j. Plain English verification report ──
        _section_header("Verification Report")
        with st.expander("Read full plain-English report", expanded=True):
            cur  = result.get("currency", "EUR")
            port = result.get("port", "")
            vsl  = result.get("vessel_name", "—")
            vnd  = result.get("vendor", "—")
            ref  = result.get("invoice_reference", "—")
            sdt  = result.get("service_date", "—")
            exp  = result.get("total_expected", 0.0)
            inv  = result.get("total_invoiced", 0.0)
            var  = result.get("total_variance", 0.0)
            var_pct_overall = (var / exp * 100) if exp else 0.0
            conf_lbl = result.get("overall_confidence_label", "")
            conf_sc  = result.get("overall_confidence", 0.0)

            # -- Overall narrative --
            verdict_prose = {
                "AUTO_APPROVED":   "All charges on this invoice match the expected tariff amounts within the allowed tolerance. No discrepancies were found.",
                "MISMATCH":        "One or more charges on this invoice differ from the expected tariff amounts by more than the allowed 1% tolerance.",
                "REVIEW_REQUIRED": "The engine was unable to fully validate one or more charges — either because no matching event was found in the SOF, or because the tariff rule requires human judgement.",
            }

            direction = "over" if var > 0 else "under"
            abs_var   = abs(var)
            abs_pct   = abs(var_pct_overall)

            report_lines = [
                f"**Invoice {ref}** · Vessel: {vsl} · Port: {port} · Vendor: {vnd} · Service date: {sdt}",
                "",
                f"**Overall result: {verdict}**",
                verdict_prose.get(verdict, ""),
                "",
                "**Financial summary**",
                f"- Expected total (from tariff): **{fmt_eur(exp, cur)}**",
                f"- Invoiced total: **{fmt_eur(inv, cur)}**",
                f"- Difference: **{fmt_eur(var, cur)}** ({abs_pct:.2f}% {direction}charged)"
                + (" — within tolerance" if abs_pct <= 1.0 else ""),
                f"- Engine confidence: **{conf_lbl}** ({conf_sc:.0%})"
                + (" — the engine had full SOF and tariff data to work with" if conf_lbl == "HIGH"
                   else " — minor data gaps; result is indicative" if conf_lbl == "MEDIUM"
                   else " — significant data gaps; treat result with caution"),
                "",
                "**Line-by-line findings**",
            ]

            for li in line_items:
                ln       = li.get("line_number", "?")
                desc     = li.get("service_description", "")
                lexp     = li.get("expected_amount", 0.0)
                linv     = li.get("invoiced_amount", 0.0)
                lvar     = li.get("variance", 0.0)
                lvar_pct = li.get("variance_pct", 0.0)
                lv       = li.get("verdict", "")
                cl       = li.get("confidence_label", "")
                cs       = li.get("confidence_score", 0.0)
                sof_ev   = li.get("sof_event_cited") or "No matching SOF event found"
                tariff_r = li.get("tariff_rule_cited") or "—"
                handler  = li.get("handler_used") or "—"
                ot       = li.get("overtime_applied")
                hr_flag  = li.get("human_review_flag", False)
                hr_rsn   = li.get("human_review_reason", "")
                surcharges = li.get("surcharges_applied") or []
                expls      = li.get("candidate_explanations") or []

                ldir = "over" if lvar > 0 else "under"
                abs_lvar = abs(lvar)
                abs_lpct = abs(lvar_pct)

                report_lines.append(f"\n**Line {ln} — {desc}**")

                if lv == "MATCH":
                    report_lines.append(
                        f"The invoiced amount of {fmt_eur(linv, cur)} matches the expected tariff amount of {fmt_eur(lexp, cur)} "
                        f"(difference: {fmt_eur(abs_lvar, cur)}, {abs_lpct:.2f}%). This line is approved."
                    )
                elif lv == "MISMATCH":
                    report_lines.append(
                        f"The vendor invoiced {fmt_eur(linv, cur)}, but the expected amount under the {port} tariff is {fmt_eur(lexp, cur)}. "
                        f"The difference is {fmt_eur(abs_lvar, cur)} ({abs_lpct:.2f}% {ldir}charged), which exceeds the 1% tolerance."
                    )
                elif lv == "UNSUPPORTED":
                    report_lines.append(
                        f"The vendor invoiced {fmt_eur(linv, cur)} for this line, but no matching event was found in the SOF to support this charge. "
                        f"The engine could not calculate an expected amount."
                    )
                elif lv == "REVIEW":
                    report_lines.append(
                        f"The vendor invoiced {fmt_eur(linv, cur)}. The engine calculated {fmt_eur(lexp, cur)} as expected, "
                        f"but flagged this line for human review. Reason: {hr_rsn or 'see notes below'}."
                    )

                report_lines.append(
                    f"- *How the expected amount was calculated:* {sof_ev}. "
                    f"Tariff: {tariff_r}. Calculation method: {handler}. Confidence: {cl} ({cs:.0%})."
                )

                if surcharges:
                    sc_texts = [f"{s.get('name')} (×{s.get('multiplier')}, {fmt_eur(s.get('amount',0), cur)})" for s in surcharges]
                    report_lines.append(f"- *Surcharges applied:* {'; '.join(sc_texts)}.")

                if ot:
                    report_lines.append(f"- *Overtime:* {ot}.")

                if expls:
                    report_lines.append("- *Possible reasons for the difference:*")
                    for ex in expls:
                        report_lines.append(
                            f"  • {ex.get('description', '')} — "
                            f"if this applies, the expected amount would be {fmt_eur(ex.get('expected_amount', 0), cur)} "
                            f"({ex.get('variance_pct', 0):+.2f}%)."
                        )
                elif lv == "MISMATCH":
                    report_lines.append(
                        "- *No automatic explanation identified.* The officer should contact the vendor or compare against "
                        "a current tariff sheet to confirm the correct rate."
                    )

            # summary notes
            notes = result.get("summary_notes") or []
            if notes:
                report_lines.append("\n**Engine notes**")
                for n in notes:
                    report_lines.append(f"- {n}")

            st.markdown("\n".join(report_lines))

        st.divider()

        # ── 8k. Approve / Escalate ─────────────
        _section_header("Officer Action")
        _needs_override_comment = verdict in ("MISMATCH", "REVIEW_REQUIRED")
        if _needs_override_comment:
            st.text_area(
                "Reason for accepting this invoice (required to approve)",
                placeholder="e.g. Zone B confirmed by port agent. Variance within acceptable range.",
                key="override_comment",
                height=80,
            )
        ba1, ba2 = st.columns(2)
        if ba1.button("✓ Approve Invoice", use_container_width=True, key="approve_btn"):
            _override_comment = st.session_state.get("override_comment", "").strip()
            if _needs_override_comment and not _override_comment:
                st.error("A reason is required to approve an invoice with a MISMATCH or REVIEW flag.")
            else:
                now_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
                existing = load_comments()
                rec_key = (
                    f"OVERRIDE::{port_key}::{result.get('invoice_reference','')}::"
                    f"{result.get('service_date','')}"
                )
                record = {
                    "_key":                  rec_key,
                    "port":                  port_key,
                    "invoice_reference":     result.get("invoice_reference", ""),
                    "vessel_name":           result.get("vessel_name", ""),
                    "service_date":          result.get("service_date", ""),
                    "line_number":           "ALL",
                    "service_description":   "Invoice-level officer approval",
                    "engine_verdict":        verdict,
                    "officer_override":      True,
                    "officer_comment":       _override_comment or "Approved — no mismatch",
                    "invoice_amount_gross":  result.get("invoice_amount_gross", 0.0),
                    "fuel_surcharge_total":  result.get("fuel_surcharge_total", 0.0),
                    "invoice_amount_net":    result.get("invoice_amount_net", 0.0),
                    "saved_at":              now_utc,
                }
                lookup = {r.get("_key"): i for i, r in enumerate(existing) if r.get("_key")}
                if rec_key in lookup:
                    existing[lookup[rec_key]] = record
                else:
                    existing.append(record)
                try:
                    save_comments(existing)
                    st.success("Invoice accepted and recorded in Officer Comments Log.")
                except Exception as e:
                    st.error(f"Failed to save approval: {e}")
        if ba2.button("⚑ Escalate for Review", use_container_width=True, key="escalate_btn"):
            st.warning("Invoice escalated for human review.")


# ══════════════════════════════════════════════
# TAB 2 — OFFICER COMMENTS LOG
# ══════════════════════════════════════════════
with tab_log:
    st.markdown("""
<div style="
    background: linear-gradient(135deg, #0d1f3c 0%, #1a3a6b 100%);
    padding: 1.5rem 2.5rem 1.4rem;
    border-radius: 12px;
    margin-bottom: 1.5rem;
    border-left: 4px solid #c9a84c;
    box-shadow: 0 6px 28px rgba(13,31,60,0.18);
">
    <div style="color:#c9a84c;font-size:0.7rem;font-weight:700;letter-spacing:0.22em;text-transform:uppercase;margin-bottom:0.4rem;">
        GET Marine Mgt
    </div>
    <div style="color:white;font-size:1.6rem;font-weight:700;font-family:Georgia,serif;">
        Officer Comments Log
    </div>
    <div style="color:#8eadd4;font-size:0.87rem;margin-top:0.4rem;">
        All saved annotations, filterable by port &nbsp;·&nbsp; Export as JSON for tariff calibration
    </div>
</div>
""", unsafe_allow_html=True)

    comments = load_comments()

    if not comments:
        st.info("No comments recorded yet. Run a verification and save officer notes to populate this log.")
    else:
        # Port filter
        ports_in_log = sorted({r.get("port", "") for r in comments if r.get("port")})
        filter_options = ["All"] + ports_in_log
        port_filter = st.selectbox("Filter by port", options=filter_options, key="log_port_filter")

        filtered = comments if port_filter == "All" else [r for r in comments if r.get("port") == port_filter]

        st.caption(f"Showing {len(filtered)} of {len(comments)} record(s).")

        # Build display rows
        display_rows = []
        for r in filtered:
            display_rows.append({
                "Saved At":           r.get("saved_at", "")[:19].replace("T", " "),
                "Port":               r.get("port", ""),
                "Invoice Ref":        r.get("invoice_reference", ""),
                "Vessel":             r.get("vessel_name", ""),
                "Date":               r.get("service_date", ""),
                "Line":               r.get("line_number", ""),
                "Description":        r.get("service_description", ""),
                "Engine Expected":    r.get("engine_expected"),
                "Invoiced":           r.get("invoiced_amount"),
                "Officer Override":   r.get("officer_expected_override"),
                "Verdict":            r.get("engine_verdict", ""),
                "Variance %":         r.get("variance_pct"),
                "Handler":            r.get("handler_used", ""),
                "Inv. Amount Net":    r.get("invoice_amount_net"),
                "Override Approved":  "YES" if r.get("officer_override") else "",
                "Comment":            r.get("officer_comment", ""),
            })

        st.dataframe(
            display_rows,
            use_container_width=True,
            hide_index=True,
        )

        # Download
        export_json = json.dumps(
            [{k: v for k, v in r.items() if k != "_key"} for r in filtered],
            indent=2,
            ensure_ascii=False,
        )
        st.download_button(
            label="⬇ Download filtered records as JSON",
            data=export_json,
            file_name=f"officer_comments_{port_filter.lower()}.json",
            mime="application/json",
            key="download_comments_btn",
        )
