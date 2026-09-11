"""Outcome precedence.

This is the function that decides whether money can move, so the ordering is argued rather
than asserted.

The five outcomes in FIN-POL-001 §3 are resolved in this order:

1. ``ESCALATE_CONTROL_REVIEW`` when two or more fraud indicators are present, or the vendor
   is under sanctions review, or a risk flag or segregation conflict was found.
2. ``REJECT_DUPLICATE`` when an exact match to a paid or posted record exists.
3. ``REJECT_INVALID`` when the request itself is unusable.
4. ``HOLD_FOR_INFORMATION`` when any blocking control failed, or a sub-threshold risk signal
   was seen.
5. ``APPROVE_FOR_POSTING`` only when every control passed.

**Why escalation outranks duplicate rejection.** Both are terminal from the supplier's point
of view, but they communicate differently. FIN-POL-007 §3 requires suspected fraud to go to
Financial Crime and Controls "without notifying the supplier of the suspicion". Rejecting an
invoice as a duplicate tells the supplier exactly which control fired, which in a payment
redirection attempt tells an attacker what to change. Escalating keeps the case inside the
control function.

**Why duplicate rejection outranks a hold.** A hold is an invitation to supply more
evidence. A settled duplicate needs no more evidence: the invoice was already paid. Holding
it leaves an open case that a later reviewer, seeing the missing receipt resolved, could
release for payment a second time.

**Why approval requires every control to pass.** There is no weighting and no partial
credit. The function returns ``APPROVE_FOR_POSTING`` only when no blocking exception exists
anywhere, and a test enumerates each control to confirm that failing any one of them removes
that outcome.

The function is total and side-effect free. It reads the sub-results and returns a decision;
it does not create approvals, call tools or write state. That separation is what lets the
approval gate be enforced by the orchestrator in one place.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from ap_agent.domain.enums import ApproverRole, Outcome
from ap_agent.domain.results import ExceptionRecord
from ap_agent.domain.rules.authority import AuthorityRequirement
from ap_agent.domain.rules.duplicates import DuplicateResult
from ap_agent.domain.rules.fraud import ESCALATION_INDICATOR_THRESHOLD, FraudIndicator
from ap_agent.domain.rules.matching import MatchResult
from ap_agent.domain.rules.vendor import VendorCheckResult


class OutcomeDecision(BaseModel):
    """The computed outcome and why.

    ``reasons`` is populated on every path, including the clean one. An audit record that
    explains only failures cannot show that a success was justified.
    """

    model_config = ConfigDict(extra="forbid")

    outcome: Outcome
    reasons: list[str] = Field(default_factory=list)
    requires_approval: bool
    requires_second_approval: bool = False
    second_approval_reason: str = ""
    required_role_minimum: ApproverRole | None = None
    blocking_exceptions: list[ExceptionRecord] = Field(default_factory=list)
    indicator_count: int = 0


def _all_exceptions(
    match: MatchResult, duplicates: DuplicateResult, vendor: VendorCheckResult
) -> list[ExceptionRecord]:
    return [*match.exceptions, *duplicates.exceptions, *vendor.exceptions]


def decide_outcome(
    *,
    match: MatchResult,
    duplicates: DuplicateResult,
    vendor: VendorCheckResult,
    authority: AuthorityRequirement,
    indicators: Sequence[FraudIndicator],
    invalid_reasons: Sequence[str] = (),
) -> OutcomeDecision:
    """Resolve the processing outcome from the control results."""
    exceptions = _all_exceptions(match, duplicates, vendor)
    blocking = [exception for exception in exceptions if exception.blocking]
    indicator_count = len(indicators)

    common = {
        "requires_second_approval": authority.requires_second_approval,
        "second_approval_reason": authority.second_approval_reason,
        "required_role_minimum": authority.required_role_minimum,
        "blocking_exceptions": blocking,
        "indicator_count": indicator_count,
    }

    # 1. Control escalation.
    escalation_reasons: list[str] = []
    if indicator_count >= ESCALATION_INDICATOR_THRESHOLD:
        escalation_reasons.append(
            f"{indicator_count} fraud indicators present, at or above the threshold of "
            f"{ESCALATION_INDICATOR_THRESHOLD} in FIN-POL-005 §3: "
            + ", ".join(indicator.code for indicator in indicators)
        )
    if vendor.requires_control_escalation:
        escalation_reasons.append(
            "the vendor record requires control review (sanctions review, a risk flag, or a "
            "segregation-of-duties conflict)"
        )
    if escalation_reasons:
        return OutcomeDecision(
            outcome=Outcome.ESCALATE_CONTROL_REVIEW,
            reasons=[
                *escalation_reasons,
                "Escalation takes precedence over rejection so that the supplier is not told "
                "which control fired (FIN-POL-007 §3).",
            ],
            requires_approval=False,
            **common,  # type: ignore[arg-type]
        )

    # 2. Settled duplicate.
    if duplicates.settled_duplicate is not None:
        record = duplicates.settled_duplicate
        return OutcomeDecision(
            outcome=Outcome.REJECT_DUPLICATE,
            reasons=[
                f"exact match to {record.record_id} ({record.invoice_reference}), already "
                f"{record.status.value}, under FIN-POL-005 §2",
                "Rejection rather than a hold: no further evidence can change the fact that "
                "the invoice was already settled.",
            ],
            requires_approval=True,
            **common,  # type: ignore[arg-type]
        )

    # 3. Unusable request.
    if invalid_reasons:
        return OutcomeDecision(
            outcome=Outcome.REJECT_INVALID,
            reasons=[*invalid_reasons, "the request cannot be processed as submitted"],
            requires_approval=True,
            **common,  # type: ignore[arg-type]
        )

    # 4. Blocking control failure, or a sub-threshold risk signal.
    if blocking:
        return OutcomeDecision(
            outcome=Outcome.HOLD_FOR_INFORMATION,
            reasons=[
                f"{exception.category.value}: {exception.failed_rule}" for exception in blocking
            ]
            + ["missing or contradictory evidence results in a hold, not a guessed conclusion"],
            requires_approval=False,
            **common,  # type: ignore[arg-type]
        )
    if indicator_count:
        return OutcomeDecision(
            outcome=Outcome.HOLD_FOR_INFORMATION,
            reasons=[
                f"{indicator_count} fraud indicator(s) below the escalation threshold: "
                + ", ".join(indicator.code for indicator in indicators),
                "recorded and held for review rather than referred to the control team",
            ],
            requires_approval=False,
            **common,  # type: ignore[arg-type]
        )

    # 5. Every control satisfied.
    reasons = [
        "three-way match within tolerance"
        if match.all_within_tolerance and match.po_present
        else "matching controls satisfied",
        "no duplicate detected against paid, posted, held or rejected history",
        f"vendor {vendor.status.value if vendor.status else 'unknown'} with no blocking flags",
        f"approval authority determined: at least {authority.required_role_minimum.value}",
        "a recommendation is not an approval (FIN-POL-001 §3), so the case stops for a human "
        "decision before anything is posted",
    ]
    return OutcomeDecision(
        outcome=Outcome.APPROVE_FOR_POSTING,
        reasons=reasons,
        requires_approval=True,
        **common,  # type: ignore[arg-type]
    )
