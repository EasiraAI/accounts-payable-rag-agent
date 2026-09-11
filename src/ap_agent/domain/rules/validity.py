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
order, it is an arithmetic fault in the supplier's own paperwork. Letting it through would feed
the tolerance engine a total that no set of lines supports, and the resulting variance would be
reported against the purchase order, blaming the wrong party.

This runs only when the submission separated its own net and tax. When it did not, the engine
substitutes the gross for the net, tax-exclusive lines fall legitimately short of it, and the
difference is the unseparated tax — which is the tax assessment's question, not a fault in the
document. A first version tested the substituted figure and rejected valid invoices for not
adding up to a total they never claimed.

**The invoice is dated in the future.** FIN-POL-006 §1 runs terms from "receipt of a valid
invoice". A date that has not yet occurred cannot be a receipt date, so the terms and the
payment schedule would both be computed from a figure known to be wrong.

**A credit note is not an invoice.** FIN-POL-008 §1 requires that a credit note "must not be
treated as a negative invoice without validating tax and accounting treatment", and §2 governs
the order in which credits are applied. Neither is implemented, and the request schema cannot
express a credit: it requires a positive amount. So a credit note can only arrive here
mistyped as an invoice, where the matching, duplicate and tax controls would all assess it
against rules written for an obligation to pay. Recognised and held rather than processed.

**Citations, stated precisely.** FIN-POL-001 §3 is what makes ``REJECT_INVALID`` an available
outcome. §2 lists the evidence a case must contain, and the minimum-evidence finding below
cites it for exactly that. §2 does *not* say the lines must sum to the document total, and it
does not say an invoice cannot be future-dated: both are integrity tests this engine applies
because an invoice that fails them cannot be reconciled meaningfully, and each finding says so
rather than claiming policy authority it does not have.

Deliberately *not* treated as invalid: an absent purchase-order reference, an absent receipt,
an absent invoice date. Each is a gap a person can close by supplying the missing item, which
makes it a hold under FIN-POL-001 §5, and each is already handled as one elsewhere. The
distinction this module draws is between a case that is incomplete and a case that is wrong.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from ap_agent.domain.enums import EscalationOwner, ExceptionCategory
from ap_agent.domain.evidence import Invoice
from ap_agent.domain.money import quantize
from ap_agent.domain.request import UntrustedText
from ap_agent.domain.results import Calculation, ExceptionRecord, PolicyFinding
from ap_agent.domain.rules._shared import next_review_date

POLICY_MINIMUM_EVIDENCE = "FIN-POL-001 §2"
POLICY_OUTCOMES = "FIN-POL-001 §3"
POLICY_TERMS = "FIN-POL-006 §1"
POLICY_CREDIT_NOTES = "FIN-POL-008 §1"
POLICY_NON_PO = "FIN-POL-012 §1"

#: Wording that identifies a credit note rather than an invoice. Matched against the document
#: reference and the case text. Using untrusted text to *hold* a case is safe in a way that
#: using it to release one is not: the worst a planted phrase achieves is a review.
_CREDIT_NOTE_TERMS: Final[tuple[str, ...]] = (
    "credit note",
    "credit memo",
    "creditnote",
    "adjustment note",
)

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
    tax_separated: bool,
    texts: Sequence[UntrustedText] = (),
    as_of: datetime,
) -> ValidityResult:
    """Decide whether the invoice is processable, and record the §2 evidence check.

    ``invoice_date_supplied`` and ``tax_separated`` are passed rather than inferred because the
    request substitutes values for both when the supplier document did not carry them. On the
    invoice object a defaulted date is indistinguishable from a real one, and a substituted net
    is indistinguishable from a stated one. Both tests below would otherwise be applied to
    figures this system invented.

    ``texts`` is the case's untrusted text, read only to recognise a credit note. It is never
    read for an assertion that would let a case proceed.
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
    else:
        absent.append("purchase-order reference")

    detail = "present: " + ", ".join(present)
    if absent:
        detail += "; absent: " + ", ".join(absent)
    if not invoice.po_reference:
        # §2 accepts "purchase-order reference or approved non-PO justification". The second
        # alternative cannot be assessed here: the request schema carries no field for it, and
        # FIN-POL-012 §1 limits non-PO processing to specific categories while §3 requires the
        # requester to supply an exception reason, receipt evidence, a cost centre and a
        # retrospective procurement review. None of those is represented. An earlier version
        # took a boolean parameter that no caller ever set, so the finding reported an absent
        # justification whether or not one existed — the same stored-and-never-read failure
        # this engine has corrected twice elsewhere.
        detail += (
            f". Whether an approved non-PO justification exists cannot be determined: the "
            f"processing request has no field for one, and {POLICY_NON_PO} requires the "
            "justification to be approved rather than asserted."
        )
    findings.append(
        PolicyFinding(
            rule="minimum_evidence_present",
            policy_ref=POLICY_MINIMUM_EVIDENCE,
            satisfied=not absent,
            detail=detail,
        )
    )

    # ---- a credit note is not an invoice (FIN-POL-008 §1) ---------------------------------
    credit_signals = sorted(
        {
            term
            for term in _CREDIT_NOTE_TERMS
            if term in invoice.invoice_reference.lower()
            or any(term in text.content.lower() for text in texts)
        }
    )
    if credit_signals:
        exceptions.append(
            ExceptionRecord(
                category=ExceptionCategory.OTHER_CONTROL_RISK,
                failed_rule="validity.document_is_an_invoice_not_a_credit_note",
                expected="an invoice representing an obligation to pay",
                observed=(
                    "the document identifies itself as a credit note ("
                    + ", ".join(credit_signals)
                    + ")"
                ),
                owner=EscalationOwner.FINANCIAL_CONTROL,
                policy_refs=[POLICY_CREDIT_NOTES, POLICY_OUTCOMES],
                next_review_date=review,
                detail=(
                    "FIN-POL-008 §1 forbids treating a credit note as a negative invoice "
                    "without validating tax and accounting treatment, and §2 governs the order "
                    "in which credits are applied. Neither is implemented here, and the "
                    "processing request cannot express a credit because it requires a positive "
                    "amount. Held for Financial Control rather than assessed against controls "
                    "written for an obligation to pay."
                ),
            )
        )
        findings.append(
            PolicyFinding(
                rule="document_is_an_invoice_not_a_credit_note",
                policy_ref=POLICY_CREDIT_NOTES,
                satisfied=False,
                detail=("Credit-note wording present: " + ", ".join(credit_signals) + ". Held."),
            )
        )

    # ---- the document must agree with its own lines --------------------------------------
    # Only when the submission stated its own net. See the module docstring: against a
    # substituted net, tax-exclusive lines fall short by the tax, which is not a fault.
    if invoice.lines and tax_separated:
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
                    policy_refs=[POLICY_OUTCOMES],
                    next_review_date=review,
                    detail=(
                        "The supplier document is internally inconsistent. This is not a variance "
                        "against the purchase order: no set of lines on this invoice supports its "
                        "stated total, so a corrected invoice is required rather than a tolerance "
                        "decision. No clause prescribes this test; it is an integrity check "
                        "without which the tolerance comparison has no meaningful input, and "
                        "FIN-POL-001 §3 is what makes REJECT_INVALID the outcome."
                    ),
                )
            )
            findings.append(
                PolicyFinding(
                    rule="document_total_agrees_with_lines",
                    policy_ref=POLICY_OUTCOMES,
                    satisfied=False,
                    detail=(
                        f"difference {difference} {invoice.currency} exceeds slack {slack}. "
                        "Engine integrity check, not a clause."
                    ),
                )
            )
        else:
            findings.append(
                PolicyFinding(
                    rule="document_total_agrees_with_lines",
                    policy_ref=POLICY_OUTCOMES,
                    satisfied=True,
                    detail=(
                        f"lines sum to {line_sum} {invoice.currency} against a document net of "
                        f"{invoice.net_amount} {invoice.currency}. Engine integrity check, not a "
                        "clause."
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
                policy_refs=[POLICY_TERMS, POLICY_OUTCOMES],
                next_review_date=review,
                detail=(
                    "FIN-POL-006 §1 runs payment terms from receipt of a valid invoice, and a "
                    "date that has not yet occurred cannot be a receipt date, so the due date "
                    "and the payment schedule would both be computed from a figure known to "
                    "be wrong. No clause states this test in terms; it is an integrity check, "
                    "and FIN-POL-001 §3 is what makes REJECT_INVALID the outcome."
                ),
            )
        )
        findings.append(
            PolicyFinding(
                rule="invoice_date_not_in_the_future",
                policy_ref=POLICY_TERMS,
                satisfied=False,
                detail=(
                    f"dated {days_ahead} day(s) ahead of the processing date. Engine integrity "
                    "check, not a clause."
                ),
            )
        )

    return ValidityResult(
        invalid_reasons=reasons,
        calculations=calculations,
        exceptions=exceptions,
        findings=findings,
    )
