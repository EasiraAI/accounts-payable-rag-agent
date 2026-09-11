"""Delegated authority tests (FIN-POL-003 v4.0).

The superseded v1.0 limits (5,000 / 25,000 / 100,000 / 500,000) appear in the corpus as
FIN-POL-003-OLD. A test at the boundary of each current limit is the check that the engine
reads the current matrix, not the historical one.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from ap_agent.domain.enums import ApproverRole, ExceptionCategory
from ap_agent.domain.evidence import DelegationRecord
from ap_agent.domain.money import Money
from ap_agent.domain.rules.authority import (
    AUTHORITY_LIMITS,
    required_authority,
    validate_approval,
)

AS_OF = datetime(2026, 9, 11, tzinfo=UTC)


def _delegation(
    *,
    delegate_role: str = "DEPARTMENT_DIRECTOR",
    starts_on: date = date(2026, 9, 1),
    ends_on: date = date(2026, 9, 30),
    revoked: bool = False,
) -> DelegationRecord:
    return DelegationRecord(
        delegation_id="DEL-2026-0044",
        register_version="4.0",
        delegate_id="U-7781",
        delegate_role=delegate_role,
        delegator_id="U-3081",
        delegator_role="DEPARTMENT_DIRECTOR",
        scope="Accounts payable invoice approval, facilities cost centre",
        starts_on=starts_on,
        ends_on=ends_on,
        revoked=revoked,
    )


class TestCurrentLimitMatrix:
    """FIN-POL-003 §2. These are the v4.0 numbers, not the superseded ones."""

    def test_limits_match_the_current_policy(self) -> None:
        assert AUTHORITY_LIMITS[ApproverRole.COST_CENTRE_MANAGER] == Decimal("10000")
        assert AUTHORITY_LIMITS[ApproverRole.DEPARTMENT_DIRECTOR] == Decimal("50000")
        assert AUTHORITY_LIMITS[ApproverRole.EXECUTIVE_DIRECTOR] == Decimal("250000")
        assert AUTHORITY_LIMITS[ApproverRole.CFO] == Decimal("1000000")

    def test_superseded_limits_are_not_used(self) -> None:
        """Guard against reading FIN-POL-003-OLD, whose director limit was 25,000."""
        assert AUTHORITY_LIMITS[ApproverRole.DEPARTMENT_DIRECTOR] != Decimal("25000")
        assert AUTHORITY_LIMITS[ApproverRole.COST_CENTRE_MANAGER] != Decimal("5000")

    @pytest.mark.parametrize(
        ("amount", "expected_role"),
        [
            ("1.00", ApproverRole.COST_CENTRE_MANAGER),
            ("10000.00", ApproverRole.COST_CENTRE_MANAGER),
            ("10000.01", ApproverRole.DEPARTMENT_DIRECTOR),
            ("50000.00", ApproverRole.DEPARTMENT_DIRECTOR),
            ("50000.01", ApproverRole.EXECUTIVE_DIRECTOR),
            ("250000.00", ApproverRole.EXECUTIVE_DIRECTOR),
            ("250000.01", ApproverRole.CFO),
            ("1000000.00", ApproverRole.CFO),
            ("1000000.01", ApproverRole.CEO),
        ],
    )
    def test_minimum_role_at_each_boundary(self, amount: str, expected_role: ApproverRole) -> None:
        requirement = required_authority(Money.of(amount, "AUD"), higher_risk_reasons=[])
        assert requirement.required_role_minimum is expected_role

    def test_requirement_records_its_calculation(self) -> None:
        requirement = required_authority(Money.of("18400.00", "AUD"), higher_risk_reasons=[])
        assert requirement.calculations
        assert any("18400.00" in str(c.inputs) for c in requirement.calculations)


class TestTotalCommitmentIncludesTax:
    """FIN-POL-003 §1: approval is based on the total commitment including tax."""

    def test_a_net_amount_below_the_limit_but_gross_above_it_escalates(self) -> None:
        gross = required_authority(Money.of("10500.00", "AUD"), higher_risk_reasons=[])
        assert gross.required_role_minimum is ApproverRole.DEPARTMENT_DIRECTOR


class TestSecondApproval:
    """FIN-POL-003 §3: two approvals, one from Financial Control."""

    def test_higher_risk_requires_a_second_approval(self) -> None:
        requirement = required_authority(
            Money.of("1000.00", "AUD"),
            higher_risk_reasons=["bank account changed within the last 30 days"],
        )
        assert requirement.requires_second_approval
        assert "bank account" in requirement.second_approval_reason

    def test_the_monetary_limit_still_applies(self) -> None:
        """ "The normal monetary limit still applies." """
        requirement = required_authority(
            Money.of("60000.00", "AUD"), higher_risk_reasons=["overseas bank account"]
        )
        assert requirement.required_role_minimum is ApproverRole.EXECUTIVE_DIRECTOR
        assert requirement.requires_second_approval

    def test_no_risk_reasons_means_one_approval(self) -> None:
        requirement = required_authority(Money.of("1000.00", "AUD"), higher_risk_reasons=[])
        assert not requirement.requires_second_approval


class TestApproverValidation:
    def test_sufficient_role_is_accepted(self) -> None:
        requirement = required_authority(Money.of("18400.00", "AUD"), higher_risk_reasons=[])
        validation = validate_approval(
            requirement,
            approver_id="U-3081",
            approver_role="DEPARTMENT_DIRECTOR",
            as_of=AS_OF,
        )
        assert validation.sufficient
        assert validation.exceptions == []

    def test_insufficient_role_raises_an_authority_gap(self) -> None:
        requirement = required_authority(Money.of("18400.00", "AUD"), higher_risk_reasons=[])
        validation = validate_approval(
            requirement,
            approver_id="U-9001",
            approver_role="COST_CENTRE_MANAGER",
            as_of=AS_OF,
        )
        assert not validation.sufficient
        assert ExceptionCategory.AUTHORITY_GAP in {e.category for e in validation.exceptions}

    def test_unknown_role_is_rejected(self) -> None:
        requirement = required_authority(Money.of("100.00", "AUD"), higher_risk_reasons=[])
        validation = validate_approval(
            requirement, approver_id="U-1", approver_role="CHIEF_VIBES_OFFICER", as_of=AS_OF
        )
        assert not validation.sufficient

    def test_financial_control_alone_cannot_satisfy_a_monetary_limit(self) -> None:
        """FIN-POL-003 §2 grants Financial Control no monetary limit of its own.

        It satisfies the "one approver must be from Financial Control" requirement in §3, not
        the limit in §2.
        """
        requirement = required_authority(Money.of("18400.00", "AUD"), higher_risk_reasons=[])
        validation = validate_approval(
            requirement, approver_id="U-4400", approver_role="FINANCIAL_CONTROL", as_of=AS_OF
        )
        assert not validation.sufficient

    def test_self_approval_is_refused(self) -> None:
        """FIN-POL-003 §1: a delegate cannot approve their own expense."""
        requirement = required_authority(Money.of("1000.00", "AUD"), higher_risk_reasons=[])
        validation = validate_approval(
            requirement,
            approver_id="U-5000",
            approver_role="CFO",
            requested_by="U-5000",
            as_of=AS_OF,
        )
        assert not validation.sufficient
        assert any("own" in reason.lower() for reason in validation.reasons)


class TestDelegation:
    """FIN-POL-003 §4."""

    def test_valid_delegation_confers_the_delegate_role(self) -> None:
        requirement = required_authority(Money.of("18400.00", "AUD"), higher_risk_reasons=[])
        validation = validate_approval(
            requirement,
            approver_id="U-7781",
            approver_role="COST_CENTRE_MANAGER",
            delegation=_delegation(),
            as_of=AS_OF,
        )
        assert validation.sufficient
        assert validation.effective_role is ApproverRole.DEPARTMENT_DIRECTOR

    def test_expired_delegation_is_invalid(self) -> None:
        """ "An expired delegation is invalid even if an earlier invoice was approved under it." """
        requirement = required_authority(Money.of("18400.00", "AUD"), higher_risk_reasons=[])
        validation = validate_approval(
            requirement,
            approver_id="U-7781",
            approver_role="COST_CENTRE_MANAGER",
            delegation=_delegation(ends_on=date(2026, 9, 1)),
            as_of=AS_OF,
        )
        assert not validation.sufficient
        assert any("expired" in reason.lower() for reason in validation.reasons)

    def test_revoked_delegation_is_invalid(self) -> None:
        requirement = required_authority(Money.of("18400.00", "AUD"), higher_risk_reasons=[])
        validation = validate_approval(
            requirement,
            approver_id="U-7781",
            approver_role="COST_CENTRE_MANAGER",
            delegation=_delegation(revoked=True),
            as_of=AS_OF,
        )
        assert not validation.sufficient

    def test_delegation_not_yet_started_is_invalid(self) -> None:
        requirement = required_authority(Money.of("18400.00", "AUD"), higher_risk_reasons=[])
        validation = validate_approval(
            requirement,
            approver_id="U-7781",
            approver_role="COST_CENTRE_MANAGER",
            delegation=_delegation(starts_on=date(2026, 10, 1), ends_on=date(2026, 10, 31)),
            as_of=AS_OF,
        )
        assert not validation.sufficient

    def test_delegation_for_a_different_person_is_not_applied(self) -> None:
        requirement = required_authority(Money.of("18400.00", "AUD"), higher_risk_reasons=[])
        validation = validate_approval(
            requirement,
            approver_id="U-0000",
            approver_role="COST_CENTRE_MANAGER",
            delegation=_delegation(),
            as_of=AS_OF,
        )
        assert not validation.sufficient
        assert any("delegate" in reason.lower() for reason in validation.reasons)

    def test_delegation_never_reduces_an_approver_own_authority(self) -> None:
        """A CFO holding a director-level delegation keeps CFO authority."""
        requirement = required_authority(Money.of("300000.00", "AUD"), higher_risk_reasons=[])
        validation = validate_approval(
            requirement,
            approver_id="U-7781",
            approver_role="CFO",
            delegation=_delegation(),
            as_of=AS_OF,
        )
        assert validation.sufficient
        assert validation.effective_role is ApproverRole.CFO


class TestApprovalEvidence:
    """FIN-POL-003 §5: the approval record must capture the applicable limit and register."""

    def test_validation_records_the_applicable_limit(self) -> None:
        requirement = required_authority(Money.of("18400.00", "AUD"), higher_risk_reasons=[])
        validation = validate_approval(
            requirement,
            approver_id="U-3081",
            approver_role="DEPARTMENT_DIRECTOR",
            as_of=AS_OF,
        )
        assert validation.applicable_limit == Decimal("50000")
        assert validation.authority_register_version
