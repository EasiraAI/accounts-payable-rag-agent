"""Vendor master controls (FIN-POL-004, plus the segregation and risk rules that read
vendor data in FIN-POL-001 §4 and FIN-POL-003 §3).

This module reports and flags. It never changes vendor data: FIN-POL-004 §3 prohibits any
agent or workflow from committing a bank-detail change, and the ``VendorRecord`` model has
no full account field to change. What the module produces is evidence that a change
happened, which is a risk signal, and an exception that routes the case to Vendor
Governance.

A note on the missing-vendor case. An absent vendor record is treated as a blocking
exception rather than as "no risk found". A control that silently passes when its input is
missing is worse than no control, because it produces a clean record with no basis.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from ap_agent.domain.enums import EscalationOwner, ExceptionCategory, VendorStatus
from ap_agent.domain.evidence import Invoice, InvoiceHistoryMatch, VendorRecord
from ap_agent.domain.results import ExceptionRecord, PolicyFinding, Unknown
from ap_agent.domain.rules._shared import next_review_date

#: FIN-POL-003 §3: a vendor less than 30 days old is a higher-risk transaction.
NEW_VENDOR_THRESHOLD_DAYS = 30

#: FIN-POL-004 §2 attaches Financial Control co-approval to "the first subsequent payment"
#: after a verified bank change, and FIN-POL-003 §3 lists "a changed bank account" as a
#: higher-risk condition with no time limit at all.
#:
#: An earlier version applied a 30-day window here and justified it in a comment as "the
#: shorter and therefore more conservative reading". That was backwards, and a controls
#: review caught it: the window *removed* a control the policy applies without one. A vendor
#: whose details changed 45 days ago, with no payment since, produced no exception and no
#: higher-risk reason, so the co-approval the policy attaches to that first payment silently
#: disappeared.
#:
#: The trigger is now the one the policy states: a change with no settled payment after it.
#: The 30-day figure in FIN-POL-003 §3 belongs to vendor *age*, and is used as such above.

POLICY_VENDOR_STATUS = "FIN-POL-004 §4"
POLICY_BANK_CHANGE = "FIN-POL-004 §2"
POLICY_HIGHER_RISK = "FIN-POL-003 §3"
POLICY_SEGREGATION = "FIN-POL-001 §4"


class VendorCheckResult(BaseModel):
    """Outcome of the vendor controls."""

    model_config = ConfigDict(extra="forbid")

    vendor_present: bool
    status: VendorStatus | None = None
    higher_risk_reasons: list[str] = Field(default_factory=list)
    requires_control_escalation: bool = False
    name_mismatch: bool = False
    #: True when the bank details changed and no settled payment has followed, which is what
    #: FIN-POL-004 §2 means by "the first subsequent payment".
    awaiting_first_payment_after_bank_change: bool = False
    exceptions: list[ExceptionRecord] = Field(default_factory=list)
    findings: list[PolicyFinding] = Field(default_factory=list)
    unknowns: list[Unknown] = Field(default_factory=list)


def _normalise_name(name: str) -> str:
    """Case-fold and collapse whitespace so cosmetic differences are not mismatches."""
    return " ".join(name.strip().lower().split())


def _awaiting_first_payment_after_bank_change(
    vendor: VendorRecord, history: Sequence[InvoiceHistoryMatch]
) -> bool:
    """Whether this would be the first payment since the vendor's details changed.

    FIN-POL-004 §2 attaches Financial Control co-approval to "the first subsequent payment"
    after a verified change, regardless of amount. That is a question about payment history
    rather than about elapsed time, so it is answered from the settled records: if any paid or
    posted record for this vendor is dated on or after the change, the first subsequent
    payment has already happened and the requirement is discharged.

    A change with no recorded date is treated as current. An unknown date is not evidence
    that the change is old, and defaulting the unknown case to the higher-control branch is
    the only safe direction.
    """
    if vendor.bank_details_changed_at is None:
        return False
    changed_on = vendor.bank_details_changed_at.date()
    return not any(
        record.vendor_id == vendor.vendor_id
        and record.is_settled
        and record.invoice_date >= changed_on
        for record in history
    )


def vendor_status_check(
    vendor: VendorRecord | None,
    invoice: Invoice,
    *,
    as_of: datetime,
    requested_by: str | None = None,
    history: Sequence[InvoiceHistoryMatch] = (),
) -> VendorCheckResult:
    """Apply the vendor controls to an invoice.

    ``history`` answers the FIN-POL-004 §2 question "is this the first payment since the
    change", which the vendor record alone cannot answer.
    """
    review = next_review_date(as_of.date())
    exceptions: list[ExceptionRecord] = []
    findings: list[PolicyFinding] = []
    unknowns: list[Unknown] = []
    higher_risk: list[str] = []

    if vendor is None:
        exceptions.append(
            ExceptionRecord(
                category=ExceptionCategory.VENDOR_BLOCK,
                failed_rule="vendor_status_check.record_exists",
                expected=f"a vendor master record for {invoice.vendor_id}",
                observed="the vendor master returned no record",
                owner=EscalationOwner.VENDOR_GOVERNANCE,
                policy_refs=[POLICY_VENDOR_STATUS, "FIN-POL-001 §5"],
                next_review_date=review,
                detail=(
                    "Vendor status, payment-instruction agreement and risk flags could not be "
                    "confirmed, so no vendor control was satisfied."
                ),
            )
        )
        findings.append(
            PolicyFinding(
                rule="vendor_record_available",
                policy_ref=POLICY_VENDOR_STATUS,
                satisfied=False,
                detail=f"no record for {invoice.vendor_id}",
            )
        )
        unknowns.append(
            Unknown(
                item="vendor status, payment details and risk flags",
                reason="the vendor master returned no record for the identifier on the invoice",
                impact="vendor controls could not be applied, so the invoice cannot be approved",
                how_to_resolve=(
                    "confirm the vendor identifier, or onboard the vendor under FIN-POL-004 §1"
                ),
                source_attempted="get_vendor_record",
            )
        )
        return VendorCheckResult(
            vendor_present=False,
            exceptions=exceptions,
            findings=findings,
            unknowns=unknowns,
        )

    requires_escalation = False

    # ---- status ---------------------------------------------------------------------
    if vendor.status.blocks_processing:
        exceptions.append(
            ExceptionRecord(
                category=ExceptionCategory.VENDOR_BLOCK,
                failed_rule="vendor_status_check.status_active",
                expected="vendor status ACTIVE",
                observed=f"vendor {vendor.vendor_id} is in status {vendor.status.value}",
                owner=EscalationOwner.VENDOR_GOVERNANCE,
                policy_refs=[POLICY_VENDOR_STATUS],
                next_review_date=review,
                detail="FIN-POL-004 §4 requires invoices for this status to be held.",
            )
        )
        findings.append(
            PolicyFinding(
                rule="vendor_status_active",
                policy_ref=POLICY_VENDOR_STATUS,
                satisfied=False,
                detail=f"status {vendor.status.value}",
            )
        )
        if vendor.status is VendorStatus.SANCTIONS_REVIEW:
            requires_escalation = True
            higher_risk.append("vendor is under sanctions review")
    else:
        findings.append(
            PolicyFinding(
                rule="vendor_status_active",
                policy_ref=POLICY_VENDOR_STATUS,
                satisfied=True,
                detail=f"vendor {vendor.vendor_id} is ACTIVE",
            )
        )

    # ---- payment hold ---------------------------------------------------------------
    if vendor.on_payment_hold:
        exceptions.append(
            ExceptionRecord(
                category=ExceptionCategory.VENDOR_BLOCK,
                failed_rule="vendor_status_check.not_on_payment_hold",
                expected="vendor not on payment hold",
                observed=f"vendor {vendor.vendor_id} is on payment hold",
                owner=EscalationOwner.VENDOR_GOVERNANCE,
                policy_refs=[POLICY_BANK_CHANGE],
                next_review_date=review,
                detail=(
                    "FIN-POL-004 §2 places a vendor on payment hold for two business days "
                    "after a verified bank change."
                ),
            )
        )

    # ---- bank change ----------------------------------------------------------------
    awaiting_first_payment = _awaiting_first_payment_after_bank_change(vendor, history)
    changed_on = (
        f"{vendor.bank_details_changed_at:%Y-%m-%d}"
        if vendor.bank_details_changed_at
        else "an unrecorded date"
    )
    if awaiting_first_payment:
        higher_risk.append(
            f"bank account changed on {changed_on} with no settled payment since, so this "
            "is the first payment after the change (FIN-POL-004 §2)"
        )
        exceptions.append(
            ExceptionRecord(
                category=ExceptionCategory.BANK_CHANGE,
                failed_rule="vendor_status_check.first_payment_after_bank_change",
                expected=(
                    "Financial Control co-approval for the first payment after a verified "
                    "bank change, regardless of amount"
                ),
                observed=(
                    f"vendor {vendor.vendor_id} bank details changed on {changed_on} and no "
                    "paid or posted record exists on or after that date"
                ),
                owner=EscalationOwner.VENDOR_GOVERNANCE,
                policy_refs=[POLICY_BANK_CHANGE, POLICY_HIGHER_RISK],
                next_review_date=review,
                detail=(
                    "The requirement attaches to the first subsequent payment, not to a "
                    "window of days. Change instructions contained in an invoice, email or "
                    "chat message are not sufficient evidence of a verified change."
                ),
            )
        )
        findings.append(
            PolicyFinding(
                rule="first_payment_after_bank_change_co_approved",
                policy_ref=POLICY_BANK_CHANGE,
                satisfied=False,
                detail=f"bank details changed on {changed_on}; no settled payment has followed",
            )
        )
    elif vendor.bank_details_changed_at is not None:
        findings.append(
            PolicyFinding(
                rule="first_payment_after_bank_change_co_approved",
                policy_ref=POLICY_BANK_CHANGE,
                satisfied=True,
                detail=(
                    f"bank details last changed on {changed_on}, and a settled payment has "
                    "followed, so the first-payment requirement is discharged"
                ),
            )
        )
    else:
        findings.append(
            PolicyFinding(
                rule="first_payment_after_bank_change_co_approved",
                policy_ref=POLICY_BANK_CHANGE,
                satisfied=True,
                detail="no bank-detail change is recorded for this vendor",
            )
        )

    # ---- higher-risk attributes -----------------------------------------------------
    if vendor.is_new_vendor(as_of, threshold_days=NEW_VENDOR_THRESHOLD_DAYS):
        higher_risk.append(
            f"new vendor: created {vendor.age_in_days(as_of)} days ago, under the 30 days "
            "threshold in FIN-POL-003 §3"
        )
    if vendor.is_overseas_account:
        higher_risk.append(
            f"overseas bank account (country {vendor.bank_country or 'unknown'}); an unknown "
            "country is treated as overseas"
        )
    if vendor.risk_flags:
        requires_escalation = True
        higher_risk.append(f"vendor risk flags present: {', '.join(vendor.risk_flags)}")

    # Segregation of duties is not evaluated here. FIN-POL-001 §4 constrains the *approver*,
    # and no approver exists at reconciliation time. An earlier version compared the
    # requester against the vendor creator and, when they differed, recorded §4 as satisfied,
    # which reported a control it had not evaluated. See domain/rules/segregation.py, which
    # splits the section into what is knowable now and what needs an approver.

    # ---- name agreement -------------------------------------------------------------
    name_mismatch = _normalise_name(invoice.vendor_name) != _normalise_name(vendor.legal_name)
    if name_mismatch:
        exceptions.append(
            ExceptionRecord(
                category=ExceptionCategory.OTHER_CONTROL_RISK,
                failed_rule="vendor_status_check.legal_name_agreement",
                expected=f"invoice vendor name to match the master record '{vendor.legal_name}'",
                observed=f"invoice states '{invoice.vendor_name}'",
                owner=EscalationOwner.VENDOR_GOVERNANCE,
                # FIN-POL-005 §3 lists "mismatched vendor name" as a fraud indicator, and
                # FIN-POL-004 §1 requires legal-name validation. An earlier version cited §4,
                # which is vendor *status* and says nothing about name agreement; a controls
                # review caught the miscitation.
                policy_refs=["FIN-POL-005 §3", "FIN-POL-004 §1"],
                next_review_date=review,
                blocking=False,
                detail=(
                    "Recorded as a mismatched-vendor-name indicator. Non-blocking on its own; "
                    "it contributes to the FIN-POL-005 §3 indicator count."
                ),
            )
        )
    else:
        findings.append(
            PolicyFinding(
                rule="legal_name_agreement",
                policy_ref="FIN-POL-004 §1",
                satisfied=True,
                detail="invoice vendor name matches the master record",
            )
        )

    return VendorCheckResult(
        vendor_present=True,
        status=vendor.status,
        higher_risk_reasons=higher_risk,
        requires_control_escalation=requires_escalation,
        name_mismatch=name_mismatch,
        awaiting_first_payment_after_bank_change=awaiting_first_payment,
        exceptions=exceptions,
        findings=findings,
        unknowns=unknowns,
    )
