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

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from ap_agent.domain.enums import EscalationOwner, ExceptionCategory, VendorStatus
from ap_agent.domain.evidence import Invoice, VendorRecord
from ap_agent.domain.results import ExceptionRecord, PolicyFinding, Unknown
from ap_agent.domain.rules._shared import next_review_date

#: FIN-POL-003 §3: a vendor less than 30 days old is a higher-risk transaction.
NEW_VENDOR_THRESHOLD_DAYS = 30

#: FIN-POL-003 §3 pairs "a changed bank account" with the new-vendor window. The same 30-day
#: horizon is applied, which is the shorter and therefore more conservative reading than
#: treating any historical change as current risk.
BANK_CHANGE_WINDOW_DAYS = 30

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
    bank_recently_changed: bool = False
    exceptions: list[ExceptionRecord] = Field(default_factory=list)
    findings: list[PolicyFinding] = Field(default_factory=list)
    unknowns: list[Unknown] = Field(default_factory=list)


def _normalise_name(name: str) -> str:
    """Case-fold and collapse whitespace so cosmetic differences are not mismatches."""
    return " ".join(name.strip().lower().split())


def vendor_status_check(
    vendor: VendorRecord | None,
    invoice: Invoice,
    *,
    as_of: datetime,
    requested_by: str | None = None,
) -> VendorCheckResult:
    """Apply the vendor controls to an invoice."""
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
    bank_recently_changed = vendor.bank_changed_recently(as_of, window_days=BANK_CHANGE_WINDOW_DAYS)
    if bank_recently_changed:
        higher_risk.append("bank account changed within the last 30 days")
        exceptions.append(
            ExceptionRecord(
                category=ExceptionCategory.BANK_CHANGE,
                failed_rule="vendor_status_check.bank_details_stable",
                expected="no bank-detail change in the 30 days before payment",
                observed=(
                    f"vendor {vendor.vendor_id} bank details changed on "
                    f"{vendor.bank_details_changed_at:%Y-%m-%d}"
                    if vendor.bank_details_changed_at
                    else "bank details changed recently"
                ),
                owner=EscalationOwner.VENDOR_GOVERNANCE,
                policy_refs=[POLICY_BANK_CHANGE, POLICY_HIGHER_RISK],
                next_review_date=review,
                detail=(
                    "The first payment after a verified bank change requires Financial "
                    "Control co-approval regardless of amount. Change instructions contained "
                    "in an invoice, email or chat message are not sufficient evidence of a "
                    "verified change."
                ),
            )
        )
        findings.append(
            PolicyFinding(
                rule="bank_details_stable",
                policy_ref=POLICY_BANK_CHANGE,
                satisfied=False,
                detail="bank details changed inside the 30-day window",
            )
        )
    else:
        findings.append(
            PolicyFinding(
                rule="bank_details_stable",
                policy_ref=POLICY_BANK_CHANGE,
                satisfied=True,
                detail="no bank-detail change inside the 30-day window",
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

    # ---- segregation of duties ------------------------------------------------------
    if requested_by and vendor.created_by and requested_by == vendor.created_by:
        exceptions.append(
            ExceptionRecord(
                category=ExceptionCategory.OTHER_CONTROL_RISK,
                failed_rule="vendor_status_check.segregation_of_duties",
                expected="the requester must not be the person who created the vendor record",
                observed=(
                    f"{requested_by} both requested this invoice and created vendor "
                    f"{vendor.vendor_id}"
                ),
                owner=EscalationOwner.FINANCIAL_CONTROL,
                policy_refs=[POLICY_SEGREGATION, "FIN-POL-003 §1"],
                next_review_date=review,
                detail="Any conflict requires escalation to Financial Control.",
            )
        )
        requires_escalation = True
    elif requested_by:
        findings.append(
            PolicyFinding(
                rule="segregation_of_duties",
                policy_ref=POLICY_SEGREGATION,
                satisfied=True,
                detail="requester and vendor creator are different people",
            )
        )

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
                policy_refs=["FIN-POL-005 §3", POLICY_VENDOR_STATUS],
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
                policy_ref=POLICY_VENDOR_STATUS,
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
        bank_recently_changed=bank_recently_changed,
        exceptions=exceptions,
        findings=findings,
        unknowns=unknowns,
    )
