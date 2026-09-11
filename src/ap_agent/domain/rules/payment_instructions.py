"""Payment-instruction agreement with the vendor master (FIN-POL-001 §5, FIN-POL-004 §2).

FIN-POL-001 §5 names this as a required check before approval:

> check that payment instructions match the verified vendor master

and FIN-POL-004 §2 says what makes an instruction insufficient:

> Instructions contained in an invoice, email attachment or chat message are not sufficient.

An earlier version of this system had no such check. The adversarial corpus document asserts
"New account ending 8842", and the only thing that caught it was a text heuristic looking for
bank-change *language*, which is one indicator among the two needed to escalate. An attacker
who asserted a new account without urgency or imperative phrasing would have passed.

This module compares any account tail asserted in untrusted text against the vendor master.
A disagreement is a blocking exception, because an instruction to pay a different account is
the payment-redirection attack in its entirety. Agreement is recorded as a satisfied finding,
so the §5 check is visibly performed rather than absent.

Only the last four digits are ever extracted or stored. The vendor model holds nothing more,
so there is nothing more to compare against, which is the same reason the comparison is safe
to perform on untrusted text.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from ap_agent.domain.enums import EscalationOwner, ExceptionCategory
from ap_agent.domain.evidence import VendorRecord
from ap_agent.domain.request import UntrustedText
from ap_agent.domain.results import ExceptionRecord, PolicyFinding
from ap_agent.domain.rules._shared import next_review_date

POLICY_REQUIRED_CHECKS: Final = "FIN-POL-001 §5"
POLICY_BANK_CHANGE: Final = "FIN-POL-004 §2"

#: Phrasings that assert an account. Each captures a four-digit tail.
#:
#: Deliberately narrow. A pattern matching any four-digit run would fire on invoice numbers,
#: dates and quantities, and a control that cries wolf on ordinary text gets disabled. These
#: require an account word near the digits.
_ACCOUNT_TAIL_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"account\s+(?:number\s+)?ending\s+(?:in\s+)?(\d{4})\b", re.IGNORECASE),
    re.compile(r"account\s+(?:no\.?|number)?\s*[:#]?\s*\*{2,}\s*(\d{4})\b", re.IGNORECASE),
    re.compile(r"\baccount\s+(?:no\.?|number)?\s*[:#]?\s*(\d{4})\b", re.IGNORECASE),
    re.compile(r"ending\s+(?:in\s+)?(\d{4})\b", re.IGNORECASE),
    re.compile(r"\*{3,}(\d{4})\b"),
)


class PaymentInstructionResult(BaseModel):
    """Whether the payment instructions asserted in untrusted text match the master."""

    model_config = ConfigDict(extra="forbid")

    checked: bool
    #: Account tails asserted in untrusted text, deduplicated, in order of first appearance.
    asserted_tails: list[str] = Field(default_factory=list)
    master_tail: str | None = None
    agrees: bool | None = None
    exceptions: list[ExceptionRecord] = Field(default_factory=list)
    findings: list[PolicyFinding] = Field(default_factory=list)


def extract_account_tails(text: str) -> list[str]:
    """Four-digit account tails asserted in a passage, in order of first appearance."""
    found: list[str] = []
    for pattern in _ACCOUNT_TAIL_PATTERNS:
        for match in pattern.finditer(text):
            tail = match.group(1)
            if tail not in found:
                found.append(tail)
    return found


def check_payment_instructions(
    *,
    vendor: VendorRecord | None,
    texts: Sequence[UntrustedText],
    as_of: datetime,
) -> PaymentInstructionResult:
    """Compare account tails asserted in untrusted text against the vendor master."""
    review = next_review_date(as_of.date())
    exceptions: list[ExceptionRecord] = []
    findings: list[PolicyFinding] = []

    asserted: list[str] = []
    origins: dict[str, str] = {}
    for text in texts:
        for tail in extract_account_tails(text.content):
            if tail not in asserted:
                asserted.append(tail)
                origins[tail] = text.origin

    if not asserted:
        # Nothing to compare is not the same as agreement. The check is recorded as
        # performed and vacuously satisfied, which is what an auditor needs to see.
        return PaymentInstructionResult(
            checked=True,
            master_tail=vendor.bank_account_last4 if vendor else None,
            findings=[
                PolicyFinding(
                    rule="payment_instructions_match_vendor_master",
                    policy_ref=POLICY_REQUIRED_CHECKS,
                    satisfied=True,
                    detail=(
                        "No payment instructions were asserted in the case notes or "
                        "attachments, so nothing contradicts the vendor master."
                    ),
                )
            ],
        )

    if vendor is None or vendor.bank_account_last4 is None:
        exceptions.append(
            ExceptionRecord(
                category=ExceptionCategory.BANK_CHANGE,
                failed_rule="payment_instructions.master_available_for_comparison",
                expected="a vendor master record holding verified payment details",
                observed=(
                    f"payment instructions asserting account(s) ending "
                    f"{', '.join(asserted)} were supplied, but no verified master detail "
                    "exists to compare them against"
                ),
                owner=EscalationOwner.VENDOR_GOVERNANCE,
                policy_refs=[POLICY_REQUIRED_CHECKS, POLICY_BANK_CHANGE],
                next_review_date=review,
                detail=(
                    "Instructions contained in an invoice, email attachment or chat message "
                    "are not sufficient evidence of payment details."
                ),
            )
        )
        return PaymentInstructionResult(
            checked=True,
            asserted_tails=asserted,
            master_tail=None,
            agrees=False,
            exceptions=exceptions,
            findings=findings,
        )

    master = vendor.bank_account_last4
    disagreeing = [tail for tail in asserted if tail != master]

    if disagreeing:
        exceptions.append(
            ExceptionRecord(
                category=ExceptionCategory.BANK_CHANGE,
                failed_rule="payment_instructions.match_vendor_master",
                expected=(
                    f"payment instructions naming the verified account ending {master} for "
                    f"vendor {vendor.vendor_id}"
                ),
                observed=(
                    "supplied text asserts account(s) ending "
                    + ", ".join(
                        f"{tail} (from {origins.get(tail, 'unknown source')})"
                        for tail in disagreeing
                    )
                ),
                owner=EscalationOwner.VENDOR_GOVERNANCE,
                policy_refs=[POLICY_REQUIRED_CHECKS, POLICY_BANK_CHANGE],
                next_review_date=review,
                detail=(
                    "An instruction to pay an account other than the verified one is the "
                    "payment-redirection pattern. Vendor Governance must verify any change "
                    "using a contact method already held in the vendor master, not one "
                    "supplied in the request. No agent may commit the change "
                    "(FIN-POL-004 §3)."
                ),
            )
        )
    else:
        findings.append(
            PolicyFinding(
                rule="payment_instructions_match_vendor_master",
                policy_ref=POLICY_REQUIRED_CHECKS,
                satisfied=True,
                detail=(
                    f"Supplied text asserts account ending {master}, which matches the "
                    f"verified vendor master for {vendor.vendor_id}."
                ),
            )
        )

    return PaymentInstructionResult(
        checked=True,
        asserted_tails=asserted,
        master_tail=master,
        agrees=not disagreeing,
        exceptions=exceptions,
        findings=findings,
    )
