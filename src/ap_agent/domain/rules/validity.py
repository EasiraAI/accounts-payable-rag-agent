"""Invoice validity and minimum evidence (FIN-POL-001 §2, §3).

This module answers one question the other rules cannot: is the submission a processable
invoice at all? Everything else in ``rules`` compares the invoice against external evidence
and, when the evidence is absent, holds. FIN-POL-001 §5 is explicit that missing evidence
produces a hold "not a guessed conclusion", so a hold is the right answer to *absence*.

``REJECT_INVALID`` is for something different, and FIN-POL-001 §3 lists it as a distinct
outcome: a document that contradicts itself or cannot describe an obligation. No amount of
retrieval repairs it, because the fault is in the submission. Holding such a case would park
it in a review queue for three business days to reach a conclusion already available now.

Two conditions are checked here. Both were chosen because they are decidable from the
submission alone, with no external lookup:

**The lines do not sum to the document total.** The per-line extension is already enforced at
the schema boundary, so a line whose quantity times unit price disagrees with its own total
never reaches this module. What the schema cannot see is the document: an invoice whose lines
sum to one figure while its net says another is not a tolerance variance against the purchase
order, it is an arithmetic fault in the supplier's own paperwork. Letting it through would
feed the tolerance engine a total that no set of lines supports, and the resulting variance
would be reported against the purchase order, blaming the wrong party.

**The invoice is dated in the future.** FIN-POL-006 §1 sets terms running from "receipt of a
valid invoice", and FIN-POL-011 §1 places an invoice in an accounting period by its date. A
date after today cannot be a receipt date, so the terms calculation and the period assignment
would both be computed from a figure known to be wrong.

Deliberately *not* treated as invalid: an absent purchase-order reference, an absent receipt,
an absent invoice date. Each is a gap a person can close by supplying the missing item, which
makes it a hold under FIN-POL-001 §5, and each is already handled as one elsewhere. The
distinction this module draws is between a case that is incomplete and a case that is wrong.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from ap_agent.domain.enums import EscalationOwner, ExceptionCategory
from ap_agent.domain.evidence import Invoice
from ap_agent.domain.money import quantize
from ap_agent.domain.results import Calculation, ExceptionRecord, PolicyFinding
from ap_agent.domain.rules._shared import next_review_date

POLICY_MINIMUM_EVIDENCE = "FIN-POL-001 §2"
POLICY_OUTCOMES = "FIN-POL-001 §3"
POLICY_TERMS = "FIN-POL-006 §1"

#: Slack on the document-total comparison, per line, in the invoice currency. A cent a line
#: absorbs a supplier's per-line rounding without admitting a real discrepancy: an arithmetic
#: fault is not a cent, and a fifty-line invoice that rounds every line the same way is still
#: only fifty cents from its own total.
_ROUNDING_SLACK_PER_LINE: Decimal = Decimal("0.01")


class ValidityResult(BaseModel):
    """Whether the submission can be processed as an invoice at all."""

    model_config = ConfigDict(extra="forbid")

    invalid_reasons: list[str] = Field(default_factory=list)
    calculations: list[Calculation] = Field(default_factory=list)
    exceptions: list[ExceptionRecord] = Field(default_factory=list)
    findings: list[PolicyFinding] = Field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not self.invalid_reasons


def check_invoice_validity(
    invoice: Invoice,
    *,
    invoice_date_supplied: bool,
    non_po_justification: bool = False,
    as_of: datetime,
) -> ValidityResult:
    """Decide whether the invoice is processable, and record the §2 evidence check.

    ``invoice_date_supplied`` is passed rather than inferred because the request substitutes
    today's date when the supplier document did not carry one. Without the flag a defaulted
    date would be indistinguishable from a real one, and the future-dating test would be
    applied to a figure this system invented.

    ``non_po_justification`` says whether an approved non-PO justification exists for a case
    with no purchase order. It is a parameter rather than a scan of the case notes because
    FIN-POL-012 §3 requires the justification to be *approved*, and text asserting one inside
    an untrusted supplier document is not evidence that it was.
    """
    review = next_review_date(as_of.date())
    reasons: list[str] = []
    calculations: list[Calculation] = []
    exceptions: list[ExceptionRecord] = []
    findings: list[PolicyFinding] = []

    # ---- FIN-POL-001 §2 minimum evidence ------------------------------------------------
    # Recorded as a finding even when satisfied. The policy names these fields explicitly, so
    # a reviewer reading the run output should be able to see that each was present rather
    # than infer it from the absence of a complaint.
    present: list[str] = ["supplier legal name", "invoice number", "currency", "gross amount"]
    absent: list[str] = []
    (present if invoice_date_supplied else absent).append("invoice date")
    if invoice.po_reference:
        present.append("purchase-order reference")
    elif non_po_justification:
        present.append("approved non-PO justification")
    else:
        absent.append("purchase-order reference or approved non-PO justification")

    detail = "present: " + ", ".join(present)
    if absent:
        detail += "; absent: " + ", ".join(absent)
    findings.append(
        PolicyFinding(
            rule="minimum_evidence_present",
            policy_ref=POLICY_MINIMUM_EVIDENCE,
            satisfied=not absent,
            detail=detail,
        )
    )

    # ---- the document must agree with its own lines --------------------------------------
    if invoice.lines:
        line_sum = quantize(sum((line.line_total for line in invoice.lines), Decimal("0")))
        slack = _ROUNDING_SLACK_PER_LINE * len(invoice.lines)
        difference = quantize(invoice.net_amount - line_sum)
        calculations.append(
            Calculation(
                name="invoice_internal_consistency",
                inputs={
                    "invoice_net": str(invoice.net_amount),
                    "sum_of_line_totals": str(line_sum),
                    "rounding_slack": str(slack),
                },
                formula="invoice_net - sum(line_total)",
                result=difference,
                currency=invoice.currency,
                policy_ref=POLICY_MINIMUM_EVIDENCE,
                note=(
                    "The invoice against itself, not against the purchase order. A document "
                    "that does not add up cannot produce a meaningful tolerance comparison."
                ),
            )
        )
        if abs(difference) > slack:
            reasons.append(
                f"the invoice lines sum to {line_sum} {invoice.currency} but the document net "
                f"is {invoice.net_amount} {invoice.currency}, a difference of {difference} "
                f"{invoice.currency}"
            )
            exceptions.append(
                ExceptionRecord(
                    category=ExceptionCategory.OTHER_CONTROL_RISK,
                    failed_rule="validity.document_total_agrees_with_lines",
                    expected=(
                        f"document net equal to the sum of lines, {line_sum} {invoice.currency}"
                    ),
                    observed=f"document net of {invoice.net_amount} {invoice.currency}",
                    owner=EscalationOwner.REQUESTER,
                    policy_refs=[POLICY_MINIMUM_EVIDENCE, POLICY_OUTCOMES],
                    next_review_date=review,
                    detail=(
                        "The supplier document is internally inconsistent. This is not a "
                        "variance against the purchase order: no set of lines on this invoice "
                        "supports its stated total, so a corrected invoice is required rather "
                        "than a tolerance decision."
                    ),
                )
            )
            findings.append(
                PolicyFinding(
                    rule="document_total_agrees_with_lines",
                    policy_ref=POLICY_MINIMUM_EVIDENCE,
                    satisfied=False,
                    detail=f"difference {difference} {invoice.currency} exceeds slack {slack}",
                )
            )
        else:
            findings.append(
                PolicyFinding(
                    rule="document_total_agrees_with_lines",
                    policy_ref=POLICY_MINIMUM_EVIDENCE,
                    satisfied=True,
                    detail=(
                        f"lines sum to {line_sum} {invoice.currency} against a document net of "
                        f"{invoice.net_amount} {invoice.currency}"
                    ),
                )
            )

    # ---- an invoice cannot be dated after it was received ---------------------------------
    if invoice_date_supplied and invoice.invoice_date > as_of.date():
        days_ahead = (invoice.invoice_date - as_of.date()).days
        reasons.append(
            f"the invoice is dated {invoice.invoice_date.isoformat()}, {days_ahead} day(s) "
            f"after the processing date {as_of.date().isoformat()}"
        )
        exceptions.append(
            ExceptionRecord(
                category=ExceptionCategory.OTHER_CONTROL_RISK,
                failed_rule="validity.invoice_date_not_in_the_future",
                expected=f"an invoice date on or before {as_of.date().isoformat()}",
                observed=invoice.invoice_date.isoformat(),
                owner=EscalationOwner.REQUESTER,
                policy_refs=[POLICY_MINIMUM_EVIDENCE, POLICY_TERMS],
                next_review_date=review,
                detail=(
                    "Payment terms run from receipt of a valid invoice under FIN-POL-006 §1, "
                    "and FIN-POL-011 §1 assigns an accounting period by invoice date. A date "
                    "that has not yet occurred cannot be either."
                ),
            )
        )
        findings.append(
            PolicyFinding(
                rule="invoice_date_not_in_the_future",
                policy_ref=POLICY_MINIMUM_EVIDENCE,
                satisfied=False,
                detail=f"dated {days_ahead} day(s) ahead of the processing date",
            )
        )

    return ValidityResult(
        invalid_reasons=reasons,
        calculations=calculations,
        exceptions=exceptions,
        findings=findings,
    )
