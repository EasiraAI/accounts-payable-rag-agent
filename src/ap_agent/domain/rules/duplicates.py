"""Duplicate invoice detection (FIN-POL-005).

The tool returns candidate history records; this module classifies them. Keeping the
classification here rather than in the tool means the matching rules are testable without
any I/O, and a different history backend cannot change what counts as a duplicate.

Two asymmetries in the policy drive the design.

**Exact and settled are separate questions.** An exact field match tells us the same invoice
was seen before. Whether it was *paid or posted* tells us whether paying again would be a
double payment. FIN-POL-005 §2 rejects only the latter, and explicitly warns that a prior
rejection does not prove a new invoice is a duplicate, because the supplier may have
corrected the fault and resubmitted legitimately.

**Fuzzy signals are weighted, not counted.** An identical attachment hash means the same
document was submitted twice, which is conclusive on its own. A near amount within a date
window is suggestive and only meaningful in combination. Treating every signal as equal
would either miss resubmitted documents or hold every recurring monthly invoice.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from ap_agent.domain.enums import EscalationOwner, ExceptionCategory, Outcome
from ap_agent.domain.evidence import Invoice, InvoiceHistoryMatch
from ap_agent.domain.money import quantize
from ap_agent.domain.results import Calculation, ExceptionRecord, PolicyFinding
from ap_agent.domain.rules._shared import next_review_date

#: FIN-POL-005 §1: "amount variance below 0.5%". The bound is strict.
FUZZY_AMOUNT_VARIANCE_PERCENT = Decimal("0.5")

#: FIN-POL-005 §1: "invoice date within 14 days".
FUZZY_DATE_WINDOW_DAYS = 14

POLICY_DETECTION = "FIN-POL-005 §1"
POLICY_OUTCOMES = "FIN-POL-005 §2"


class DuplicateResult(BaseModel):
    """Classification of the candidate history records."""

    model_config = ConfigDict(extra="forbid")

    checked: bool
    exact_matches: list[InvoiceHistoryMatch] = Field(default_factory=list)
    fuzzy_matches: list[InvoiceHistoryMatch] = Field(default_factory=list)
    settled_duplicate: InvoiceHistoryMatch | None = None
    recommended_outcome: Outcome | None = None
    calculations: list[Calculation] = Field(default_factory=list)
    exceptions: list[ExceptionRecord] = Field(default_factory=list)
    findings: list[PolicyFinding] = Field(default_factory=list)

    @property
    def has_any_match(self) -> bool:
        return bool(self.exact_matches or self.fuzzy_matches)


def _amount_variance_percent(candidate: Decimal, prior: Decimal) -> Decimal:
    """Absolute variance between two amounts as a percentage of the prior amount."""
    if prior == 0:
        return Decimal("100")
    return (abs(candidate - prior) / prior * Decimal("100")).quantize(Decimal("0.0001"))


def _normalise_reference(reference: str) -> str:
    """An invoice number reduced to its alphanumeric characters, upper-cased.

    Shared by the exact and the fuzzy test so the two cannot disagree about what counts as
    the same number. FIN-POL-005 §1 requires punctuation-stripped comparison; keeping one
    implementation is what makes "INV-1001", "INV 1001" and "inv1001" one invoice number
    everywhere in this module.
    """
    return "".join(char for char in reference if char.isalnum()).upper()


def _is_exact(invoice: Invoice, record: InvoiceHistoryMatch) -> bool:
    """FIN-POL-005 §1 exact match: vendor, normalised number, currency and gross amount."""
    return (
        invoice.vendor_id == record.vendor_id
        and invoice.normalised_reference == _normalise_reference(record.invoice_reference)
        and invoice.currency == record.currency
        and invoice.gross_amount == record.gross_amount
    )


def _fuzzy_reasons(invoice: Invoice, record: InvoiceHistoryMatch) -> list[str]:
    """Fuzzy signals present between the candidate and one prior record.

    Returns the reasons that, taken together, make this a probable match. An empty list means
    the record is not a probable duplicate.
    """
    if invoice.vendor_id != record.vendor_id:
        # A different vendor is not a duplicate under any of the §1 signals, all of which are
        # scoped to a vendor. Without this guard, two suppliers billing the same round amount
        # in the same week would look like a duplicate.
        return []

    shared_hashes = set(invoice.attachment_hashes) & set(record.attachment_hashes)
    if shared_hashes:
        # Conclusive on its own: the identical document was submitted twice.
        return [f"identical attachment hash {sorted(shared_hashes)[0][:12]}"]

    if invoice.normalised_reference == _normalise_reference(record.invoice_reference):
        # FIN-POL-005 §1 names "punctuation-stripped invoice numbers" as a fuzzy signal in
        # its own right. An earlier version reached this comparison only through the exact
        # test, which also requires the currency and gross amount to agree, so a supplier who
        # resubmitted "INV-1001" as "INV 1001" with a corrected amount matched neither test
        # and was processed as a new invoice. The same vendor reusing an invoice number is
        # strong on its own: invoice numbers are a supplier's own sequence, and a repeat is
        # either a resubmission or a numbering fault. Either way it warrants a look.
        return [
            f"same invoice number once punctuation is stripped "
            f"({invoice.normalised_reference}), amount "
            f"{_amount_variance_percent(invoice.gross_amount, record.gross_amount)}% apart"
        ]

    variance_percent = _amount_variance_percent(invoice.gross_amount, record.gross_amount)
    amount_is_near = variance_percent < FUZZY_AMOUNT_VARIANCE_PERCENT
    days_apart = abs((invoice.invoice_date - record.invoice_date).days)
    within_window = days_apart <= FUZZY_DATE_WINDOW_DAYS
    same_po = bool(invoice.po_reference) and invoice.po_reference == record.po_reference

    if not amount_is_near:
        # Every remaining signal is only meaningful alongside a near amount. Two invoices
        # against the same purchase order for genuinely different amounts are normal
        # instalments, not duplicates.
        return []

    reasons: list[str] = []
    if within_window:
        reasons.append(
            f"amount variance {variance_percent}% below {FUZZY_AMOUNT_VARIANCE_PERCENT}% and "
            f"invoice dates {days_apart} day(s) apart, within the "
            f"{FUZZY_DATE_WINDOW_DAYS}-day window"
        )
    if same_po:
        reasons.append(
            f"same purchase order {invoice.po_reference} with amount variance {variance_percent}%"
        )
    return reasons


def duplicate_check(
    invoice: Invoice,
    history: Sequence[InvoiceHistoryMatch],
    *,
    as_of: datetime,
) -> DuplicateResult:
    """Classify candidate history records against the invoice under assessment."""
    review = next_review_date(as_of.date())
    calculations: list[Calculation] = []
    exceptions: list[ExceptionRecord] = []
    findings: list[PolicyFinding] = []
    exact: list[InvoiceHistoryMatch] = []
    fuzzy: list[InvoiceHistoryMatch] = []

    for record in history:
        if _is_exact(invoice, record):
            exact.append(
                record.model_copy(
                    update={
                        "match_type": "exact",
                        "match_reasons": [
                            "vendor, normalised invoice number, currency and gross amount all match"
                        ],
                    }
                )
            )
            continue
        reasons = _fuzzy_reasons(invoice, record)
        if reasons:
            fuzzy.append(
                record.model_copy(update={"match_type": "fuzzy", "match_reasons": reasons})
            )
            calculations.append(
                Calculation(
                    name=f"duplicate_amount_variance_{record.record_id}",
                    inputs={
                        "candidate_gross": str(invoice.gross_amount),
                        "prior_gross": str(record.gross_amount),
                        "threshold_percent": str(FUZZY_AMOUNT_VARIANCE_PERCENT),
                    },
                    formula="abs(candidate_gross - prior_gross) / prior_gross x 100",
                    result=quantize(
                        _amount_variance_percent(invoice.gross_amount, record.gross_amount)
                    ),
                    currency=None,
                    policy_ref=POLICY_DETECTION,
                    note="Percentage, not currency.",
                )
            )

    settled = next((record for record in exact if record.is_settled), None)

    recommended: Outcome | None = None
    if settled is not None:
        recommended = Outcome.REJECT_DUPLICATE
        exceptions.append(
            ExceptionRecord(
                category=ExceptionCategory.DUPLICATE_RISK,
                failed_rule="duplicate_check.exact_match_to_settled_record",
                expected=(
                    f"no paid or posted record for invoice {invoice.invoice_reference} "
                    f"from vendor {invoice.vendor_id}"
                ),
                observed=(
                    f"record {settled.record_id} (invoice {settled.invoice_reference}, "
                    f"{settled.gross_amount} {settled.currency}) is already {settled.status.value}"
                ),
                owner=EscalationOwner.ACCOUNTS_PAYABLE_MANAGER,
                policy_refs=[POLICY_DETECTION, POLICY_OUTCOMES],
                next_review_date=review,
                detail=(
                    "Exact match on vendor, normalised invoice number, currency and gross "
                    "amount against a settled record. Paying again would be a double payment."
                ),
            )
        )
        findings.append(
            PolicyFinding(
                rule="no_settled_duplicate",
                policy_ref=POLICY_OUTCOMES,
                satisfied=False,
                detail=f"settled record {settled.record_id} matches exactly",
            )
        )
    elif exact or fuzzy:
        recommended = Outcome.HOLD_FOR_INFORMATION
        matched = exact + fuzzy
        reference_list = ", ".join(
            f"{record.record_id} ({record.invoice_reference}, {record.status.value})"
            for record in matched
        )
        exceptions.append(
            ExceptionRecord(
                category=ExceptionCategory.DUPLICATE_RISK,
                failed_rule="duplicate_check.probable_match",
                expected=f"no prior record resembling invoice {invoice.invoice_reference}",
                observed=f"probable match against {reference_list}",
                owner=EscalationOwner.ACCOUNTS_PAYABLE_MANAGER,
                policy_refs=[POLICY_DETECTION, POLICY_OUTCOMES],
                next_review_date=review,
                detail=(
                    "A probable match is held rather than rejected. A prior rejection or hold "
                    "does not prove this invoice is a duplicate; the reason and any corrected "
                    "fields must be reviewed (FIN-POL-005 §2)."
                ),
            )
        )
        findings.append(
            PolicyFinding(
                rule="no_settled_duplicate",
                policy_ref=POLICY_OUTCOMES,
                satisfied=True,
                detail="no paid or posted record matches exactly, but a probable match exists",
            )
        )
    else:
        findings.append(
            PolicyFinding(
                rule="no_duplicate_detected",
                policy_ref=POLICY_DETECTION,
                satisfied=True,
                detail=(
                    f"{len(history)} candidate record(s) examined against paid, posted, held "
                    "and rejected history; none matched"
                ),
            )
        )

    return DuplicateResult(
        checked=True,
        exact_matches=exact,
        fuzzy_matches=fuzzy,
        settled_duplicate=settled,
        recommended_outcome=recommended,
        calculations=calculations,
        exceptions=exceptions,
        findings=findings,
    )
