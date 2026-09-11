"""Delegated financial authority (FIN-POL-003 v4.0).

The limits in this module are the current matrix. The corpus also contains
``FIN-POL-003-OLD`` with lower limits (5,000 / 25,000 / 100,000 / 500,000) and
``status: superseded``. A retrieval system can rank the superseded document lower, but it
cannot guarantee the model ignores it. The limits are therefore constants in code, tested at
every boundary, and the retrieved policy text is used for citation rather than for values.
That is the concrete meaning of "policy is code" in this system.

Financial Control deserves a note. FIN-POL-003 §2 lists no monetary limit for it, while §3
requires that when two approvals are needed "one approver must be from Financial Control".
Financial Control therefore satisfies the second-approver requirement and never the limit.
Treating it as unlimited would invert the control: the role that exists to add oversight
would become the one able to approve anything alone.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from ap_agent.domain.enums import ApproverRole, EscalationOwner, ExceptionCategory
from ap_agent.domain.evidence import DelegationRecord
from ap_agent.domain.money import Money, MoneyAmount
from ap_agent.domain.results import Calculation, ExceptionRecord, PolicyFinding
from ap_agent.domain.rules._shared import next_review_date

#: FIN-POL-003 §2, version 4.0. Amounts are the maximum total commitment including tax.
AUTHORITY_LIMITS: dict[ApproverRole, Decimal] = {
    ApproverRole.COST_CENTRE_MANAGER: Decimal("10000"),
    ApproverRole.DEPARTMENT_DIRECTOR: Decimal("50000"),
    ApproverRole.EXECUTIVE_DIRECTOR: Decimal("250000"),
    ApproverRole.CFO: Decimal("1000000"),
}

#: Roles in ascending order of authority. ``CEO`` carries no ceiling ("above AUD 1,000,000").
AUTHORITY_LADDER: tuple[ApproverRole, ...] = (
    ApproverRole.COST_CENTRE_MANAGER,
    ApproverRole.DEPARTMENT_DIRECTOR,
    ApproverRole.EXECUTIVE_DIRECTOR,
    ApproverRole.CFO,
    ApproverRole.CEO,
)

#: The version of the matrix these constants encode. Recorded on every approval per §5.
AUTHORITY_REGISTER_VERSION = "FIN-POL-003 v4.0"

POLICY_LIMITS = "FIN-POL-003 §2"
POLICY_HIGHER_RISK = "FIN-POL-003 §3"
POLICY_DELEGATION = "FIN-POL-003 §4"
POLICY_EVIDENCE = "FIN-POL-003 §5"
POLICY_GENERAL = "FIN-POL-003 §1"


class AuthorityRequirement(BaseModel):
    """What this transaction needs before it can be posted."""

    model_config = ConfigDict(extra="forbid")

    amount: MoneyAmount
    currency: str
    required_role_minimum: ApproverRole
    applicable_limit: MoneyAmount | None
    requires_second_approval: bool = False
    second_approval_reason: str = ""
    authority_register_version: str = AUTHORITY_REGISTER_VERSION
    calculations: list[Calculation] = Field(default_factory=list)
    findings: list[PolicyFinding] = Field(default_factory=list)


class AuthorityValidation(BaseModel):
    """Whether a specific approver satisfies the requirement."""

    model_config = ConfigDict(extra="forbid")

    sufficient: bool
    effective_role: ApproverRole | None
    applicable_limit: MoneyAmount | None
    authority_register_version: str = AUTHORITY_REGISTER_VERSION
    delegation_applied: str | None = None
    reasons: list[str] = Field(default_factory=list)
    exceptions: list[ExceptionRecord] = Field(default_factory=list)
    findings: list[PolicyFinding] = Field(default_factory=list)


def _minimum_role_for(amount: Decimal) -> ApproverRole:
    """Lowest role whose limit covers the amount.

    The comparison is ``amount <= limit``, so an amount exactly at a limit is within that
    role's authority and does not escalate. "Maximum approval" in §2 is inclusive.
    """
    for role in AUTHORITY_LADDER:
        limit = AUTHORITY_LIMITS.get(role)
        if limit is None:
            return role  # CEO: no ceiling
        if amount <= limit:
            return role
    return ApproverRole.CEO


def _parse_role(raw: str) -> ApproverRole | None:
    try:
        return ApproverRole(raw.strip().upper())
    except ValueError:
        return None


def required_authority(
    total: Money,
    *,
    higher_risk_reasons: Sequence[str],
) -> AuthorityRequirement:
    """Determine the approval requirement for a total commitment.

    ``total`` must already include tax and related charges (FIN-POL-003 §1). The caller is
    responsible for converting a foreign-currency invoice to the policy currency with a cited
    rate under FIN-POL-009 §2; this function does not convert, because inventing a rate here
    would produce an authority decision with no auditable basis.
    """
    role = _minimum_role_for(total.amount)
    limit = AUTHORITY_LIMITS.get(role)
    requires_second = bool(higher_risk_reasons)

    calculations = [
        Calculation(
            name="required_approval_authority",
            inputs={
                "total_commitment": str(total.amount),
                "currency": total.currency,
                "register_version": AUTHORITY_REGISTER_VERSION,
                "limit_for_role": str(limit) if limit is not None else "no ceiling",
            },
            formula="lowest role in the FIN-POL-003 §2 matrix whose maximum >= total_commitment",
            result=limit if limit is not None else total.amount,
            currency=total.currency,
            policy_ref=POLICY_LIMITS,
            note=(
                "Total commitment includes tax and related charges. Amounts must not be split "
                "to avoid a threshold (FIN-POL-003 §1)."
            ),
        )
    ]

    findings = [
        PolicyFinding(
            rule="approval_authority_determined",
            policy_ref=POLICY_LIMITS,
            satisfied=True,
            detail=(
                f"{total.amount} {total.currency} requires at least {role.value}"
                + (f" (limit {limit} {total.currency})" if limit is not None else " (no ceiling)")
            ),
        )
    ]
    if requires_second:
        findings.append(
            PolicyFinding(
                rule="second_approval_required",
                policy_ref=POLICY_HIGHER_RISK,
                satisfied=False,
                detail=(
                    "higher-risk transaction: two approvals are required, one from Financial "
                    "Control, and the normal monetary limit still applies"
                ),
            )
        )

    return AuthorityRequirement(
        amount=total.amount,
        currency=total.currency,
        required_role_minimum=role,
        applicable_limit=limit,
        requires_second_approval=requires_second,
        second_approval_reason="; ".join(higher_risk_reasons),
        calculations=calculations,
        findings=findings,
    )


def validate_approval(
    requirement: AuthorityRequirement,
    *,
    approver_id: str,
    approver_role: str,
    delegation: DelegationRecord | None = None,
    requested_by: str | None = None,
    as_of: datetime,
) -> AuthorityValidation:
    """Check that a named approver may approve this transaction.

    Evaluated in this order, because each check can disqualify the approver outright:
    self-approval, role recognition, delegation validity, then the monetary limit.
    """
    review = next_review_date(as_of.date())
    reasons: list[str] = []
    exceptions: list[ExceptionRecord] = []
    findings: list[PolicyFinding] = []

    # ---- self-approval ---------------------------------------------------------------
    if requested_by and approver_id == requested_by:
        reasons.append(
            "the approver raised this request, and a delegate cannot approve their own "
            "expense or a purchase they personally benefit from"
        )
        exceptions.append(
            ExceptionRecord(
                category=ExceptionCategory.AUTHORITY_GAP,
                failed_rule="validate_approval.no_self_approval",
                expected="an approver who is not the requester",
                observed=f"{approver_id} is both requester and approver",
                owner=EscalationOwner.FINANCIAL_CONTROL,
                policy_refs=[POLICY_GENERAL],
                next_review_date=review,
            )
        )
        return AuthorityValidation(
            sufficient=False,
            effective_role=None,
            applicable_limit=requirement.applicable_limit,
            reasons=reasons,
            exceptions=exceptions,
            findings=findings,
        )

    # ---- role recognition ------------------------------------------------------------
    own_role = _parse_role(approver_role)
    if own_role is None:
        reasons.append(f"role '{approver_role}' is not in the FIN-POL-003 §2 matrix")
        exceptions.append(
            ExceptionRecord(
                category=ExceptionCategory.AUTHORITY_GAP,
                failed_rule="validate_approval.role_recognised",
                expected="a role listed in the delegated authority matrix",
                observed=f"role '{approver_role}'",
                owner=EscalationOwner.FINANCIAL_CONTROL,
                policy_refs=[POLICY_LIMITS],
                next_review_date=review,
            )
        )
        return AuthorityValidation(
            sufficient=False,
            effective_role=None,
            applicable_limit=requirement.applicable_limit,
            reasons=reasons,
            exceptions=exceptions,
            findings=findings,
        )

    # ---- delegation ------------------------------------------------------------------
    effective_role = own_role
    delegation_applied: str | None = None
    if delegation is not None:
        if delegation.delegate_id != approver_id:
            reasons.append(
                f"delegation {delegation.delegation_id} names delegate "
                f"{delegation.delegate_id}, not {approver_id}"
            )
        else:
            invalid_reason = delegation.invalid_reason_at(as_of.date())
            if invalid_reason:
                reasons.append(invalid_reason)
                exceptions.append(
                    ExceptionRecord(
                        category=ExceptionCategory.AUTHORITY_GAP,
                        failed_rule="validate_approval.delegation_current",
                        expected="a delegation current at the decision date",
                        observed=invalid_reason,
                        owner=EscalationOwner.FINANCIAL_CONTROL,
                        policy_refs=[POLICY_DELEGATION],
                        next_review_date=review,
                        detail=(
                            "An expired delegation is invalid even if an earlier invoice was "
                            "approved under it."
                        ),
                    )
                )
            else:
                delegated_role = _parse_role(delegation.delegate_role)
                if delegated_role is None:
                    reasons.append(
                        f"delegation {delegation.delegation_id} confers unrecognised role "
                        f"'{delegation.delegate_role}'"
                    )
                else:
                    # A delegation grants authority; it never removes the approver's own.
                    candidates = [own_role, delegated_role]
                    effective_role = max(
                        candidates,
                        key=lambda role: (
                            AUTHORITY_LIMITS.get(role, Decimal("Infinity"))
                            if role is not ApproverRole.FINANCIAL_CONTROL
                            else Decimal("-1")
                        ),
                    )
                    if effective_role is delegated_role and delegated_role is not own_role:
                        delegation_applied = delegation.delegation_id
                        findings.append(
                            PolicyFinding(
                                rule="delegation_applied",
                                policy_ref=POLICY_DELEGATION,
                                satisfied=True,
                                detail=(
                                    f"delegation {delegation.delegation_id} from "
                                    f"{delegation.delegator_id} confers {delegated_role.value} "
                                    f"until {delegation.ends_on.isoformat()}"
                                ),
                            )
                        )

    # ---- monetary limit --------------------------------------------------------------
    if effective_role is ApproverRole.FINANCIAL_CONTROL:
        reasons.append(
            "Financial Control carries no monetary limit in FIN-POL-003 §2; it satisfies the "
            "second-approver requirement in §3, not the limit"
        )
        exceptions.append(
            ExceptionRecord(
                category=ExceptionCategory.AUTHORITY_GAP,
                failed_rule="validate_approval.role_carries_a_monetary_limit",
                expected=f"an approver holding at least {requirement.required_role_minimum.value}",
                observed="Financial Control acting as sole approver",
                owner=EscalationOwner.FINANCIAL_CONTROL,
                policy_refs=[POLICY_LIMITS, POLICY_HIGHER_RISK],
                next_review_date=review,
            )
        )
        return AuthorityValidation(
            sufficient=False,
            effective_role=effective_role,
            applicable_limit=requirement.applicable_limit,
            delegation_applied=delegation_applied,
            reasons=reasons,
            exceptions=exceptions,
            findings=findings,
        )

    effective_limit = AUTHORITY_LIMITS.get(effective_role)
    sufficient = effective_limit is None or requirement.amount <= effective_limit

    if sufficient and not reasons:
        findings.append(
            PolicyFinding(
                rule="approver_within_authority",
                policy_ref=POLICY_EVIDENCE,
                satisfied=True,
                detail=(
                    f"{approver_id} as {effective_role.value} may approve "
                    f"{requirement.amount} {requirement.currency}"
                    + (
                        f" against a limit of {effective_limit} {requirement.currency}"
                        if effective_limit is not None
                        else " (no ceiling)"
                    )
                    + f", register {AUTHORITY_REGISTER_VERSION}"
                ),
            )
        )
    elif not sufficient:
        reasons.append(
            f"{effective_role.value} limit of {effective_limit} {requirement.currency} is below "
            f"the total commitment of {requirement.amount} {requirement.currency}"
        )
        exceptions.append(
            ExceptionRecord(
                category=ExceptionCategory.AUTHORITY_GAP,
                failed_rule="validate_approval.within_monetary_limit",
                expected=(
                    f"an approver holding at least {requirement.required_role_minimum.value} "
                    f"for {requirement.amount} {requirement.currency}"
                ),
                observed=(
                    f"{approver_id} holds {effective_role.value} with a limit of "
                    f"{effective_limit} {requirement.currency}"
                ),
                owner=EscalationOwner.FINANCIAL_CONTROL,
                policy_refs=[POLICY_LIMITS],
                next_review_date=review,
                detail="Amounts must not be split to avoid a threshold (FIN-POL-003 §1).",
            )
        )

    # A delegation that named someone else, or an unrecognised delegated role, leaves the
    # approver on their own authority but the discrepancy is still reported.
    if reasons and sufficient:
        sufficient = False

    return AuthorityValidation(
        sufficient=sufficient,
        effective_role=effective_role,
        applicable_limit=effective_limit,
        delegation_applied=delegation_applied,
        reasons=reasons,
        exceptions=exceptions,
        findings=findings,
    )
