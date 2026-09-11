"""Segregation of duties (FIN-POL-001 §4, FIN-POL-003 §1).

The policy states two distinct requirements, and they are checked at different moments
because they need different facts:

> The person who creates or changes a vendor record must not **approve** an invoice for that
> vendor during the following five business days. The **requester, receipter and financial
> approver must be distinct** for invoices above AUD 25,000.

The first constrains the *approver*, so it can only be evaluated when an approver is known,
which is at approval time and not during reconciliation. The second needs all three parties,
and the approver is again the missing one until a callback arrives.

An earlier version of this module compared the **requester** against the vendor creator and,
when they differed, emitted a satisfied finding citing FIN-POL-001 §4. That was wrong twice
over. It tested a relationship the policy does not constrain, and it reported the section as
satisfied when neither of the section's actual requirements had been evaluated. A green
finding that names a control nobody checked is worse than a missing finding, because it
invites reliance.

So this module splits the work explicitly. ``check_reconciliation_time`` reports only what
can be known before an approver exists and says so in its detail text.
``check_approval_time`` evaluates both requirements once the approver is known, and the
orchestrator refuses the approval if either fails.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from ap_agent.domain.enums import EscalationOwner, ExceptionCategory
from ap_agent.domain.evidence import Invoice, PurchaseOrder, VendorRecord
from ap_agent.domain.results import ExceptionRecord, PolicyFinding
from ap_agent.domain.rules._shared import add_business_days, next_review_date

#: FIN-POL-001 §4: "during the following five business days".
VENDOR_CHANGE_COOLING_OFF_BUSINESS_DAYS: Final = 5

#: FIN-POL-001 §4: "must be distinct for invoices above AUD 25,000". Strictly above.
THREE_PARTY_DISTINCTNESS_THRESHOLD: Final = Decimal("25000")

POLICY_SEGREGATION: Final = "FIN-POL-001 §4"
POLICY_GENERAL_AUTHORITY: Final = "FIN-POL-003 §1"


class SegregationResult(BaseModel):
    """Outcome of the segregation checks performed at one point in a run."""

    model_config = ConfigDict(extra="forbid")

    satisfied: bool
    #: Parties the check was able to compare. Named so a finding can state its own scope
    #: rather than implying the whole section was evaluated.
    parties_compared: list[str] = Field(default_factory=list)
    #: Requirements this call could not evaluate, and why.
    deferred: list[str] = Field(default_factory=list)
    exceptions: list[ExceptionRecord] = Field(default_factory=list)
    findings: list[PolicyFinding] = Field(default_factory=list)


def _receipters(purchase_order: PurchaseOrder | None) -> set[str]:
    if purchase_order is None:
        return set()
    return {receipt.receipted_by for receipt in purchase_order.receipts}


def _three_party_applies(invoice: Invoice) -> bool:
    """FIN-POL-001 §4 applies the distinctness rule above AUD 25,000.

    The comparison is strictly greater than, matching "above". An invoice exactly at the
    threshold is not above it.
    """
    return invoice.gross_amount > THREE_PARTY_DISTINCTNESS_THRESHOLD


def check_reconciliation_time(
    *,
    vendor: VendorRecord | None,
    purchase_order: PurchaseOrder | None,
    invoice: Invoice,
    requested_by: str | None,
) -> SegregationResult:
    """Evaluate the parts of §4 that do not need an approver.

    Only one relationship is knowable here: whether the requester is also a receipter, which
    the three-party rule forbids above the threshold. Everything involving the approver is
    reported as deferred, in the finding's own text, so a reader cannot mistake this for a
    full evaluation of the section.
    """
    exceptions: list[ExceptionRecord] = []
    findings: list[PolicyFinding] = []
    compared: list[str] = []
    deferred: list[str] = []

    receipters = _receipters(purchase_order)
    applies = _three_party_applies(invoice)

    if applies and requested_by and requested_by in receipters:
        compared.append("requester vs receipter")
        exceptions.append(
            ExceptionRecord(
                category=ExceptionCategory.OTHER_CONTROL_RISK,
                failed_rule="segregation.requester_distinct_from_receipter",
                expected=(
                    "requester, receipter and financial approver to be distinct for an "
                    f"invoice above {THREE_PARTY_DISTINCTNESS_THRESHOLD} "
                    f"{invoice.currency}"
                ),
                observed=f"{requested_by} both raised the request and receipted the goods",
                owner=EscalationOwner.FINANCIAL_CONTROL,
                policy_refs=[POLICY_SEGREGATION],
                detail="Any conflict requires escalation to Financial Control.",
            )
        )
    elif applies and requested_by:
        compared.append("requester vs receipter")

    if applies:
        deferred.append(
            "approver distinctness from the requester and receipter, which needs an approver"
        )
    deferred.append(
        "the five-business-day bar on the vendor's creator approving, which needs an approver"
    )

    # The finding states exactly what was compared and what was not. It is recorded as
    # satisfied only when something was actually compared and passed.
    if compared and not exceptions:
        findings.append(
            PolicyFinding(
                rule="segregation_of_duties_partial",
                policy_ref=POLICY_SEGREGATION,
                satisfied=True,
                detail=(
                    "Checked at reconciliation: "
                    + "; ".join(compared)
                    + ". Not yet checked, pending an approver: "
                    + "; ".join(deferred)
                    + "."
                ),
            )
        )
    elif not compared:
        findings.append(
            PolicyFinding(
                rule="segregation_of_duties_partial",
                policy_ref=POLICY_SEGREGATION,
                satisfied=True,
                detail=(
                    "Nothing comparable at reconciliation: "
                    + (
                        f"the invoice is at or below {THREE_PARTY_DISTINCTNESS_THRESHOLD} "
                        f"{invoice.currency}, so the three-party rule does not apply. "
                        if not applies
                        else "no requester was supplied. "
                    )
                    + "Pending an approver: "
                    + "; ".join(deferred)
                    + "."
                ),
            )
        )

    return SegregationResult(
        satisfied=not exceptions,
        parties_compared=compared,
        deferred=deferred,
        exceptions=exceptions,
        findings=findings,
    )


def check_approval_time(
    *,
    vendor: VendorRecord | None,
    purchase_order: PurchaseOrder | None,
    invoice: Invoice,
    requested_by: str | None,
    approver_id: str,
    existing_signatories: Sequence[str] = (),
    as_of: datetime,
) -> SegregationResult:
    """Evaluate FIN-POL-001 §4 in full, now that an approver is known.

    Three checks:

    1. The vendor's creator or last editor may not approve within five business days of that
       change. The window runs from ``last_updated_at`` when the record has been edited,
       otherwise from ``created_at``, because the policy says "creates or changes".
    2. Above the threshold, requester, receipter and approver must be distinct.
    Distinctness of signatories is deliberately *not* checked here. It is enforced by the
    signature table's ``(approval_id, approver_id)`` primary key and by counting distinct
    signatories towards the requirement, which is where it belongs: a duplicate delivery from
    the same approver is an ordinary at-least-once retry and must be an idempotent replay, not
    an error. Raising on it would turn a normal redelivery into a failure.
    """
    exceptions: list[ExceptionRecord] = []
    findings: list[PolicyFinding] = []
    compared: list[str] = []
    review = next_review_date(as_of.date())

    # 1. Cooling-off period after a vendor create or change.
    if vendor is not None and vendor.created_by == approver_id:
        changed_at = vendor.last_updated_at or vendor.created_at
        bar_until = add_business_days(changed_at.date(), VENDOR_CHANGE_COOLING_OFF_BUSINESS_DAYS)
        compared.append("approver vs vendor creator")
        if as_of.date() <= bar_until:
            exceptions.append(
                ExceptionRecord(
                    category=ExceptionCategory.OTHER_CONTROL_RISK,
                    failed_rule="segregation.vendor_editor_may_not_approve_within_five_days",
                    expected=(
                        f"an approver other than {approver_id}, who created or last changed "
                        f"vendor {vendor.vendor_id}, until "
                        f"{bar_until.isoformat()}"
                    ),
                    observed=(
                        f"{approver_id} created or last changed the vendor record on "
                        f"{changed_at.date().isoformat()} and is approving on "
                        f"{as_of.date().isoformat()}"
                    ),
                    owner=EscalationOwner.FINANCIAL_CONTROL,
                    policy_refs=[POLICY_SEGREGATION, POLICY_GENERAL_AUTHORITY],
                    next_review_date=review,
                    detail=(
                        "FIN-POL-001 §4 bars the person who creates or changes a vendor "
                        "record from approving an invoice for that vendor during the "
                        "following five business days."
                    ),
                )
            )

    # 2. Three-party distinctness above the threshold.
    if _three_party_applies(invoice):
        receipters = _receipters(purchase_order)
        compared.append("approver vs requester and receipter")
        clashes: list[str] = []
        if requested_by and approver_id == requested_by:
            clashes.append(f"{approver_id} is both requester and approver")
        if approver_id in receipters:
            clashes.append(f"{approver_id} is both receipter and approver")
        if clashes:
            exceptions.append(
                ExceptionRecord(
                    category=ExceptionCategory.OTHER_CONTROL_RISK,
                    failed_rule="segregation.three_parties_distinct",
                    expected=(
                        "requester, receipter and financial approver to be three distinct "
                        f"people for an invoice above {THREE_PARTY_DISTINCTNESS_THRESHOLD} "
                        f"{invoice.currency}"
                    ),
                    observed="; ".join(clashes),
                    owner=EscalationOwner.FINANCIAL_CONTROL,
                    policy_refs=[POLICY_SEGREGATION],
                    next_review_date=review,
                    detail=(
                        f"Invoice gross {invoice.gross_amount} {invoice.currency} is above "
                        f"the {THREE_PARTY_DISTINCTNESS_THRESHOLD} threshold, so the "
                        "three-party rule applies."
                    ),
                )
            )

    # Signatory distinctness is enforced by the store, not here. See the docstring.
    if existing_signatories:
        compared.append(f"approver against {len(existing_signatories)} existing signature(s)")

    if compared and not exceptions:
        findings.append(
            PolicyFinding(
                rule="segregation_of_duties",
                policy_ref=POLICY_SEGREGATION,
                satisfied=True,
                detail="Checked with the approver known: " + "; ".join(compared) + ".",
            )
        )

    return SegregationResult(
        satisfied=not exceptions,
        parties_compared=compared,
        deferred=[],
        exceptions=exceptions,
        findings=findings,
    )
