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

#: The cost centre the delegations below are scoped to. FIN-POL-003 §4 makes scope a
#: mandatory part of a register entry, and it is now compared: a delegation outside its
#: scope confers nothing.
CASE_COST_CENTRE = "CC-4100 Industrial Maintenance"


def _delegation(
    *,
    delegate_role: str = "DEPARTMENT_DIRECTOR",
    starts_on: date = date(2026, 9, 1),
    ends_on: date = date(2026, 9, 30),
    revoked: bool = False,
    scope: str = "Accounts payable invoice approval, CC-4100 Industrial Maintenance",
) -> DelegationRecord:
    return DelegationRecord(
        delegation_id="DEL-2026-0044",
        register_version="4.0",
        delegate_id="U-7781",
        delegate_role=delegate_role,
        delegator_id="U-3081",
        delegator_role="DEPARTMENT_DIRECTOR",
        scope=scope,
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
            requested_by="U-2210",
            case_cost_centre=CASE_COST_CENTRE,
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
            requested_by="U-2210",
            case_cost_centre=CASE_COST_CENTRE,
            as_of=AS_OF,
        )
        assert not validation.sufficient
        assert ExceptionCategory.AUTHORITY_GAP in {e.category for e in validation.exceptions}

    def test_unknown_role_is_rejected(self) -> None:
        requirement = required_authority(Money.of("100.00", "AUD"), higher_risk_reasons=[])
        validation = validate_approval(
            requirement,
            approver_id="U-1",
            approver_role="CHIEF_VIBES_OFFICER",
            requested_by="U-2210",
            case_cost_centre=CASE_COST_CENTRE,
            as_of=AS_OF,
        )
        assert not validation.sufficient

    def test_financial_control_alone_cannot_satisfy_a_monetary_limit(self) -> None:
        """FIN-POL-003 §2 grants Financial Control no monetary limit of its own.

        It satisfies the "one approver must be from Financial Control" requirement in §3, not
        the limit in §2.
        """
        requirement = required_authority(Money.of("18400.00", "AUD"), higher_risk_reasons=[])
        validation = validate_approval(
            requirement,
            approver_id="U-4400",
            approver_role="FINANCIAL_CONTROL",
            requested_by="U-2210",
            case_cost_centre=CASE_COST_CENTRE,
            as_of=AS_OF,
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
            requested_by="U-2210",
            case_cost_centre=CASE_COST_CENTRE,
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
            requested_by="U-2210",
            case_cost_centre=CASE_COST_CENTRE,
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
            requested_by="U-2210",
            case_cost_centre=CASE_COST_CENTRE,
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
            requested_by="U-2210",
            case_cost_centre=CASE_COST_CENTRE,
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
            requested_by="U-2210",
            case_cost_centre=CASE_COST_CENTRE,
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
            requested_by="U-2210",
            case_cost_centre=CASE_COST_CENTRE,
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
            requested_by="U-2210",
            case_cost_centre=CASE_COST_CENTRE,
            as_of=AS_OF,
        )
        assert validation.applicable_limit == Decimal("50000")
        assert validation.authority_register_version


class TestRequesterMustBeKnown:
    """S2: the self-approval control was defeated by omitting an optional field.

    ``requested_by`` is caller-supplied, so an absent value is a gap in the evidence, not a
    clean bill of health. A security review omitted it and approved its own request.
    """

    def test_an_absent_requester_refuses_the_approval(self) -> None:
        requirement = required_authority(Money.of("1000.00", "AUD"), higher_risk_reasons=[])
        validation = validate_approval(
            requirement,
            approver_id="U-5000",
            approver_role="CFO",
            requested_by=None,
            case_cost_centre=CASE_COST_CENTRE,
            as_of=AS_OF,
        )
        assert not validation.sufficient
        assert any("does not identify a requester" in reason for reason in validation.reasons)
        assert ExceptionCategory.AUTHORITY_GAP in {e.category for e in validation.exceptions}

    def test_an_empty_requester_is_treated_as_absent(self) -> None:
        requirement = required_authority(Money.of("1000.00", "AUD"), higher_risk_reasons=[])
        validation = validate_approval(
            requirement,
            approver_id="U-5000",
            approver_role="CFO",
            requested_by="",
            case_cost_centre=CASE_COST_CENTRE,
            as_of=AS_OF,
        )
        assert not validation.sufficient


class TestDelegationScope:
    """S8: FIN-POL-003 §4 makes scope mandatory, and it was stored but never compared."""

    def test_a_delegation_outside_its_scope_confers_nothing(self) -> None:
        requirement = required_authority(Money.of("18400.00", "AUD"), higher_risk_reasons=[])
        validation = validate_approval(
            requirement,
            approver_id="U-7781",
            approver_role="COST_CENTRE_MANAGER",
            delegation=_delegation(),
            requested_by="U-2210",
            case_cost_centre="CC-9000 Treasury",
            as_of=AS_OF,
        )
        assert not validation.sufficient
        assert any("scoped to" in reason for reason in validation.reasons)

    def test_an_unknown_cost_centre_cannot_verify_a_scoped_delegation(self) -> None:
        """A delegation whose applicability cannot be verified is not one that applies."""
        requirement = required_authority(Money.of("18400.00", "AUD"), higher_risk_reasons=[])
        validation = validate_approval(
            requirement,
            approver_id="U-7781",
            approver_role="COST_CENTRE_MANAGER",
            delegation=_delegation(),
            requested_by="U-2210",
            case_cost_centre=None,
            as_of=AS_OF,
        )
        assert not validation.sufficient

    def test_a_scope_naming_no_restriction_is_unrestricted(self) -> None:
        """An entry that names no restriction imposes none."""
        requirement = required_authority(Money.of("18400.00", "AUD"), higher_risk_reasons=[])
        validation = validate_approval(
            requirement,
            approver_id="U-7781",
            approver_role="COST_CENTRE_MANAGER",
            delegation=_delegation(scope="Accounts payable invoice approval"),
            requested_by="U-2210",
            case_cost_centre=None,
            as_of=AS_OF,
        )
        assert validation.sufficient


class TestForeignCurrencyAuthority:
    """FIN-POL-009 §2 requires authority to be assessed in AUD with a cited rate."""

    def test_a_foreign_currency_amount_records_the_substitution(self) -> None:
        requirement = required_authority(Money.of("300000.00", "USD"), higher_risk_reasons=[])
        assert requirement.assumptions
        assert "FIN-POL-009 §2" in requirement.assumptions[0]
        assert any(
            finding.rule == "authority_assessed_in_policy_currency" and not finding.satisfied
            for finding in requirement.findings
        )

    def test_a_policy_currency_amount_records_no_substitution(self) -> None:
        requirement = required_authority(Money.of("300000.00", "AUD"), higher_risk_reasons=[])
        assert requirement.assumptions == []


class TestFinancialControlFlag:
    def test_financial_control_is_flagged_as_such(self) -> None:
        """So the gate can tell whether the FIN-POL-003 §3 co-approver requirement is met."""
        requirement = required_authority(Money.of("100.00", "AUD"), higher_risk_reasons=[])
        validation = validate_approval(
            requirement,
            approver_id="U-4400",
            approver_role="FINANCIAL_CONTROL",
            requested_by="U-2210",
            case_cost_centre=CASE_COST_CENTRE,
            as_of=AS_OF,
        )
        assert validation.is_financial_control
