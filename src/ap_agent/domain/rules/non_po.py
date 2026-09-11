"""Repeated non-PO purchasing (FIN-POL-012 §4).

FIN-POL-012 §4 is short and specific: "Two or more non-PO invoices from the same supplier in
90 days must be escalated for sourcing review. The agent should retrieve prior cases before
recommending an exception."

It is the one control in the corpus that is about a *pattern* rather than a transaction. A
single invoice with no purchase order may be a statutory charge, a utility or a genuine
emergency, all of which §1 permits. The same supplier arriving repeatedly without one is a
procurement problem: a contract that should exist and does not, or a spending relationship
being run outside the purchasing system one invoice at a time. Neither invoice looks wrong on
its own, which is exactly why a per-transaction control cannot see it.

The inputs are already in the run. The history tool retrieves prior records for duplicate
detection, and those records carry the purchase-order reference and the invoice date, so the
"retrieve prior cases" half of §4 costs nothing extra here — the second sentence of §4 is
satisfied by the evidence phase that has already run.

**This escalates rather than blocks.** §4 sends the case to a sourcing review, which is a
procurement matter concerning the relationship, not a reason to withhold payment for goods
already received. Holding the invoice would penalise the supplier for a fault on the buying
side. The exception is recorded, it names the prior cases, and the payment decision is taken
on the invoice's own merits.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field

from ap_agent.domain.enums import EscalationOwner, ExceptionCategory
from ap_agent.domain.evidence import Invoice, InvoiceHistoryMatch
from ap_agent.domain.results import ExceptionRecord, PolicyFinding
from ap_agent.domain.rules._shared import next_review_date

POLICY_REPEATED_USE = "FIN-POL-012 §4"
POLICY_NON_PO = "FIN-POL-012 §1"

#: FIN-POL-012 §4: "in 90 days". Calendar days, measured back from the invoice under
#: assessment rather than from the processing date, so a case that sat in a queue is judged on
#: the purchasing pattern at the time it was raised.
REPEAT_WINDOW_DAYS = 90

#: FIN-POL-012 §4: "Two or more non-PO invoices". The invoice under assessment is one of them,
#: so one prior record inside the window reaches the threshold.
REPEAT_THRESHOLD = 2


class NonPoResult(BaseModel):
    """Whether this supplier is being invoiced repeatedly without a purchase order."""

    model_config = ConfigDict(extra="forbid")

    assessed: bool = False
    prior_non_po_records: list[str] = Field(default_factory=list)
    exceptions: list[ExceptionRecord] = Field(default_factory=list)
    findings: list[PolicyFinding] = Field(default_factory=list)


def check_repeated_non_po(
    invoice: Invoice,
    history: Sequence[InvoiceHistoryMatch],
    *,
    as_of: datetime,
) -> NonPoResult:
    """Apply FIN-POL-012 §4 to the invoice under assessment.

    Returns an unassessed result for a PO-backed invoice. The control is about the absence of
    a purchase order, so an invoice that has one is outside its scope entirely, and recording
    a satisfied finding for every ordinary invoice would bury the cases that matter.
    """
    if invoice.po_reference:
        return NonPoResult(assessed=False)

    review = next_review_date(as_of.date())
    window_start = invoice.invoice_date - timedelta(days=REPEAT_WINDOW_DAYS)
    prior = [
        record
        for record in history
        if record.vendor_id == invoice.vendor_id
        and not record.po_reference
        and window_start <= record.invoice_date <= invoice.invoice_date
    ]
    # The invoice under assessment counts towards the threshold: §4 says "two or more non-PO
    # invoices", and this is one of them.
    total = len(prior) + 1
    if total < REPEAT_THRESHOLD:
        return NonPoResult(
            assessed=True,
            findings=[
                PolicyFinding(
                    rule="non_po_use_not_repeated",
                    policy_ref=POLICY_REPEATED_USE,
                    satisfied=True,
                    detail=(
                        f"No other non-PO invoice from this vendor in the "
                        f"{REPEAT_WINDOW_DAYS} days before "
                        f"{invoice.invoice_date.isoformat()}."
                    ),
                )
            ],
        )

    record_ids = [record.record_id for record in prior]
    return NonPoResult(
        assessed=True,
        prior_non_po_records=record_ids,
        exceptions=[
            ExceptionRecord(
                category=ExceptionCategory.OTHER_CONTROL_RISK,
                failed_rule="non_po.repeated_use_within_window",
                expected=(
                    f"fewer than {REPEAT_THRESHOLD} non-PO invoices from this vendor in "
                    f"{REPEAT_WINDOW_DAYS} days"
                ),
                observed=(
                    f"{total} including this one: " + ", ".join(record_ids) + " and the case "
                    "under assessment"
                ),
                owner=EscalationOwner.ACCOUNTS_PAYABLE_MANAGER,
                policy_refs=[POLICY_REPEATED_USE, POLICY_NON_PO],
                next_review_date=review,
                # Not blocking. §4 calls for a sourcing review, which is about the purchasing
                # relationship rather than this invoice. The goods or services were received;
                # withholding payment would penalise the supplier for a gap on the buying
                # side. The payment decision is taken on the invoice's own merits.
                blocking=False,
                detail=(
                    "Repeated non-PO purchasing from one supplier is a procurement gap rather "
                    "than an invoice fault: a contract or standing order should carry this "
                    "spend. FIN-POL-012 §4 requires escalation for sourcing review, and §1 "
                    "limits non-PO processing to statutory charges, approved utilities, "
                    "property leases, approved professional memberships and genuine "
                    "emergencies."
                ),
            )
        ],
        findings=[
            PolicyFinding(
                rule="non_po_use_not_repeated",
                policy_ref=POLICY_REPEATED_USE,
                satisfied=False,
                detail=(
                    f"{total} non-PO invoices from this vendor within {REPEAT_WINDOW_DAYS} "
                    f"days: {', '.join(record_ids)} and this case."
                ),
            )
        ],
    )
