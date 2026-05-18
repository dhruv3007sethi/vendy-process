"""
output_formatter.py
--------------------
Structures the calculation results from all modules into a
standardised output for:
  1. The agent UI (Retool) — verdict, confidence, line item breakdown
  2. The audit trail (SQLite) — full citation chain per line item

Input: results from handler + surcharge_engine + overtime_calculator
Output: FormattedResult dataclass with all fields needed by UI and DB

Confidence scoring:
  HIGH   (>=90%) : All inputs from SOF, exact tariff match, no human review flags
  MEDIUM (70-89%): Minor gaps (e.g. zone inferred, tug spec from invoice)
  LOW    (<70%)  : Missing SOF events, open doubts, human review required
"""

import logging
from dataclasses import dataclass, field
from typing import List, Optional, Any, Dict
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RESULT DATACLASSES
# ---------------------------------------------------------------------------

@dataclass
class LineItemResult:
    """Result for a single invoice line item."""
    line_number: int
    service_description: str        # from invoice
    sof_event_cited: str            # SOF event that supports this charge
    tariff_rule_cited: str          # tariff clause / table / formula used
    expected_amount: float          # what the system calculated
    invoiced_amount: float          # what vendor charged
    currency: str
    variance: float                 # invoiced - expected
    variance_pct: float             # (variance / expected) * 100
    verdict: str                    # "MATCH" | "MISMATCH" | "UNSUPPORTED" | "REVIEW" | "ADJUSTMENT"
    confidence_score: float         # 0.0 to 1.0
    confidence_label: str           # "HIGH" | "MEDIUM" | "LOW"
    handler_used: str               # which calculation handler produced this
    surcharges_applied: List[Dict]  # list of dicts (name, amount, citation)
    overtime_applied: Optional[str] # OvertimeResult citation if applicable
    human_review_flag: bool
    human_review_reason: str
    notes: str
    candidate_explanations: List[Dict] = field(default_factory=list)


@dataclass
class FormattedResult:
    """Full result for one invoice validation run."""
    invoice_reference: str
    vendor: str
    port: str
    vessel_name: str
    vessel_dimension_type: str      # LOA | GT | GRT
    vessel_dimension_value: float
    service_date: str
    currency: str                   # Added for UI header consistency
    validated_at: str               # ISO timestamp

    line_items: List[LineItemResult] = field(default_factory=list)

    total_expected: float = 0.0
    total_invoiced: float = 0.0
    total_variance: float = 0.0
    overall_verdict: str = ""       # "AUTO_APPROVED" | "REVIEW_REQUIRED" | "MISMATCH"
    overall_confidence: float = 0.0
    overall_confidence_label: str = ""
    human_review_required: bool = False
    summary_notes: List[str] = field(default_factory=list)

    invoice_amount_gross: float = 0.0   # sum of service lines (before discounts/surcharges)
    fuel_surcharge_total: float = 0.0   # bunker/fuel adjustment lines
    invoice_amount_net: float = 0.0     # gross minus discount adjustment lines


# ---------------------------------------------------------------------------
# CONFIDENCE SCORING
# ---------------------------------------------------------------------------

def _score_confidence(
    sof_event_found: bool,
    exact_tariff_match: bool,
    human_review_flag: bool,
    has_open_doubts: bool,
    tug_spec_from_invoice: bool = False,
    zone_inferred: bool = False
) -> tuple[float, str]:
    """
    Returns (score, label) based on data quality signals.

    Deductions:
      - No SOF event found          : -0.30
      - No exact tariff match        : -0.15
      - Human review flag set        : -0.15
      - Zone inferred (not from SOF) : -0.05

    Not deducted (intentionally excluded):
      - has_open_doubts     : port-level doubt flags are too coarse for per-line scoring
      - tug_spec_from_invoice: tug count nearly always comes from the invoice; not a data gap
    """
    score = 1.0

    if not sof_event_found:
        score -= 0.30
    if not exact_tariff_match:
        score -= 0.15
    if human_review_flag:
        score -= 0.15
    if zone_inferred:
        score -= 0.05

    # Clamp score between 0 and 1
    score = round(max(0.0, min(1.0, score)), 2)

    if score >= 0.90:
        label = "HIGH"
    elif score >= 0.70:
        label = "MEDIUM"
    else:
        label = "LOW"

    return score, label


# ---------------------------------------------------------------------------
# VERDICT DETERMINATION
# ---------------------------------------------------------------------------

def _determine_verdict(
    variance_pct: float,
    human_review_flag: bool,
    sof_event_found: bool,
    match_tolerance_pct: float = 1.0
) -> str:
    """
    Assigns a verdict to a single line item.

    MATCH        : variance within tolerance and no flags
    MISMATCH     : variance outside tolerance
    UNSUPPORTED  : no SOF event found to support this charge
    REVIEW       : human review flag set regardless of variance
    """
    if not sof_event_found:
        return "UNSUPPORTED"
    if human_review_flag:
        return "REVIEW"
    if abs(variance_pct) <= match_tolerance_pct:
        return "MATCH"
    return "MISMATCH"


def _determine_overall_verdict(line_items: List[LineItemResult]) -> str:
    """
    Rolls up line item verdicts into an overall invoice verdict.

    AUTO_APPROVED  : all lines MATCH, no human review flags
    REVIEW_REQUIRED: at least one REVIEW or UNSUPPORTED line
    MISMATCH       : at least one MISMATCH line

    ADJUSTMENT lines are excluded — they are accepted on face value and
    do not represent tariff-validated charges.
    """
    tariff_lines = [li for li in line_items if li.verdict != "ADJUSTMENT"]
    if not tariff_lines:
        return "REVIEW_REQUIRED"

    verdicts = [li.verdict for li in tariff_lines]

    if "MISMATCH" in verdicts:
        return "MISMATCH"
    if "REVIEW" in verdicts or "UNSUPPORTED" in verdicts:
        return "REVIEW_REQUIRED"
    return "AUTO_APPROVED"


# ---------------------------------------------------------------------------
# LINE ITEM BUILDER
# ---------------------------------------------------------------------------

def build_line_item(
    line_number: int,
    service_description: str,
    invoiced_amount: float,
    currency: str,
    handler_result: Dict[str, Any],
    sof_event_cited: str,
    tariff_rule_cited: str,
    handler_used: str,
    surcharge_report=None,       # SurchargeReport or None
    overtime_result=None,        # OvertimeResult or None
    human_review_flag: bool = False,
    human_review_reason: str = "",
    sof_event_found: bool = True,
    exact_tariff_match: bool = True,
    has_open_doubts: bool = False,
    tug_spec_from_invoice: bool = False,
    zone_inferred: bool = False,
    notes: str = "",
    candidate_explanations: List[Dict] = None,
    match_tolerance_pct: float = 1.0
) -> LineItemResult:
    """
    Builds a single LineItemResult from handler + surcharge + overtime outputs.

    Args:
        handler_result  : dict returned by any calculation handler
        surcharge_report: SurchargeReport from surcharge_engine (or None)
        overtime_result : OvertimeResult from overtime_calculator (or None)
    """
    # 1. Determine Base Rate
    base = float(handler_result.get('total_rate') or 0.0)

    # 2. Add Surcharges
    surcharge_total = surcharge_report.total_surcharge_amount if surcharge_report else 0.0
    surcharges_list = []
    
    if surcharge_report:
        # Convert SurchargeResult objects to dicts for serialization
        surcharges_list = [
            {
                "name": s.name, 
                "multiplier": s.multiplier,
                "amount": s.surcharge_amount, 
                "citation": s.citation
            }
            for s in surcharge_report.surcharges
        ]

    # 3. Add Overtime
    overtime_total = 0.0
    overtime_citation = None
    if overtime_result:
        overtime_total = overtime_result.overtime_charge
        overtime_citation = overtime_result.citation

    # 4. Final Math
    expected_amount = round(base + surcharge_total + overtime_total, 2)
    variance = round(invoiced_amount - expected_amount, 2)
    
    # Prevent division by zero
    if expected_amount != 0:
        variance_pct = round((variance / expected_amount * 100), 2)
    else:
        if invoiced_amount != 0:
            variance_pct = 100.0  # 100% error if calc is 0 but invoiced is not
        else:
            variance_pct = 0.0
            # Both zero — no baseline to compare; force review
            human_review_flag = True
            if not human_review_reason:
                human_review_reason = "Expected and invoiced both zero — no baseline"

    # 5. Verdict & Confidence
    verdict = _determine_verdict(
        variance_pct=variance_pct,
        human_review_flag=human_review_flag,
        sof_event_found=sof_event_found,
        match_tolerance_pct=match_tolerance_pct
    )

    confidence_score, confidence_label = _score_confidence(
        sof_event_found=sof_event_found,
        exact_tariff_match=exact_tariff_match,
        human_review_flag=human_review_flag,
        has_open_doubts=has_open_doubts,
        tug_spec_from_invoice=tug_spec_from_invoice,
        zone_inferred=zone_inferred
    )

    if verdict in ["UNSUPPORTED", "REVIEW"]:
        logger.info(f"Line {line_number} ({service_description}) flagged as {verdict}: {human_review_reason or 'No SOF'}")

    return LineItemResult(
        line_number=line_number,
        service_description=service_description,
        sof_event_cited=sof_event_cited,
        tariff_rule_cited=tariff_rule_cited,
        expected_amount=expected_amount,
        invoiced_amount=invoiced_amount,
        currency=currency,
        variance=variance,
        variance_pct=variance_pct,
        verdict=verdict,
        confidence_score=confidence_score,
        confidence_label=confidence_label,
        handler_used=handler_used,
        surcharges_applied=surcharges_list,
        overtime_applied=overtime_citation,
        human_review_flag=human_review_flag,
        human_review_reason=human_review_reason,
        notes=notes,
        candidate_explanations=candidate_explanations or []
    )


# ---------------------------------------------------------------------------
# FULL RESULT BUILDER
# ---------------------------------------------------------------------------

def build_result(
    invoice_reference: str,
    vendor: str,
    port: str,
    vessel_name: str,
    vessel_dimension_type: str,
    vessel_dimension_value: float,
    service_date: str,
    line_items: List[LineItemResult],
    currency: str = "EUR",
    invoice_amount_gross: float = 0.0,
    fuel_surcharge_total: float = 0.0,
    invoice_amount_net: float = 0.0,
) -> FormattedResult:
    """
    Assembles a FormattedResult from a list of LineItemResults.
    Calculates totals, overall verdict, and overall confidence.
    """
    # Calculate Financial Totals — exclude ADJUSTMENT lines (non-tariff charges accepted on face value)
    tariff_lines = [li for li in line_items if li.verdict != "ADJUSTMENT"]
    total_expected = round(sum(li.expected_amount for li in tariff_lines), 2)
    total_invoiced = round(sum(li.invoiced_amount for li in tariff_lines), 2)
    total_variance = round(total_invoiced - total_expected, 2)

    # Determine Overall Verdict
    overall_verdict = _determine_overall_verdict(line_items)
    human_review_required = overall_verdict != "AUTO_APPROVED"

    # Calculate Overall Confidence (Average) — exclude ADJUSTMENT lines
    if tariff_lines:
        overall_confidence = round(
            sum(li.confidence_score for li in tariff_lines) / len(tariff_lines), 2
        )
    else:
        overall_confidence = 0.0

    # Map confidence score to label
    if overall_confidence >= 0.90:
        overall_confidence_label = "HIGH"
    elif overall_confidence >= 0.70:
        overall_confidence_label = "MEDIUM"
    else:
        overall_confidence_label = "LOW"

    # Generate Summary Notes
    summary_notes = []
    for li in line_items:
        if li.human_review_reason:
            summary_notes.append(f"Line {li.line_number}: {li.human_review_reason}")
        if li.verdict == "UNSUPPORTED":
            summary_notes.append(
                f"Line {li.line_number}: '{li.service_description}' — "
                "no supporting SOF event found"
            )
        for explanation in li.candidate_explanations:
            summary_notes.append(f"Line {li.line_number}: {explanation.get('description', '')}")

    return FormattedResult(
        invoice_reference=invoice_reference,
        vendor=vendor,
        port=port,
        vessel_name=vessel_name,
        vessel_dimension_type=vessel_dimension_type,
        vessel_dimension_value=vessel_dimension_value,
        service_date=service_date,
        currency=currency,
        validated_at=datetime.now(timezone.utc).isoformat(),
        line_items=line_items,
        total_expected=total_expected,
        total_invoiced=total_invoiced,
        total_variance=total_variance,
        overall_verdict=overall_verdict,
        overall_confidence=overall_confidence,
        overall_confidence_label=overall_confidence_label,
        human_review_required=human_review_required,
        summary_notes=summary_notes,
        invoice_amount_gross=invoice_amount_gross,
        fuel_surcharge_total=fuel_surcharge_total,
        invoice_amount_net=invoice_amount_net,
    )


# ---------------------------------------------------------------------------
# SERIALISER — for Retool UI and SQLite storage
# ---------------------------------------------------------------------------

def to_dict(result: FormattedResult) -> dict:
    """
    Converts a FormattedResult to a plain dict for JSON serialisation
    (Retool API response or SQLite storage).
    """
    return {
        "invoice_reference": result.invoice_reference,
        "vendor": result.vendor,
        "port": result.port,
        "vessel_name": result.vessel_name,
        "vessel_dimension_type": result.vessel_dimension_type,
        "vessel_dimension_value": result.vessel_dimension_value,
        "service_date": result.service_date,
        "currency": result.currency,
        "validated_at": result.validated_at,
        "overall_verdict": result.overall_verdict,
        "overall_confidence": result.overall_confidence,
        "overall_confidence_label": result.overall_confidence_label,
        "total_expected": result.total_expected,
        "total_invoiced": result.total_invoiced,
        "total_variance": result.total_variance,
        "human_review_required": result.human_review_required,
        "summary_notes": result.summary_notes,
        "invoice_amount_gross": result.invoice_amount_gross,
        "fuel_surcharge_total": result.fuel_surcharge_total,
        "invoice_amount_net": result.invoice_amount_net,
        "line_items": [
            {
                "line_number": li.line_number,
                "service_description": li.service_description,
                "sof_event_cited": li.sof_event_cited,
                "tariff_rule_cited": li.tariff_rule_cited,
                "expected_amount": li.expected_amount,
                "invoiced_amount": li.invoiced_amount,
                "currency": li.currency,
                "variance": li.variance,
                "variance_pct": li.variance_pct,
                "verdict": li.verdict,
                "confidence_score": li.confidence_score,
                "confidence_label": li.confidence_label,
                "handler_used": li.handler_used,
                "surcharges_applied": li.surcharges_applied, # Already a list of dicts
                "overtime_applied": li.overtime_applied,
                "human_review_flag": li.human_review_flag,
                "human_review_reason": li.human_review_reason,
                "notes": li.notes,
                "candidate_explanations": li.candidate_explanations
            }
            for li in result.line_items
        ]
    }

