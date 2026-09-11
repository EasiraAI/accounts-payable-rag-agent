"""Outcome precedence tests.

The precedence order is the single most consequential piece of logic in the system: it is
what decides whether money can move. Each test states the policy reason for the ordering it
asserts.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from ap_agent.domain.enums import (
    ApproverRole,
    EscalationOwner,
    ExceptionCategory,
    Outcome,
    VendorStatus,
)
from ap_agent.domain.evidence import InvoiceHistoryMatch, InvoiceHistoryStatus
from ap_agent.domain.money import Money
from ap_agent.domain.results import ExceptionRecord
from ap_agent.domain.rules.authority import required_authority
from ap_agent.domain.rules.duplicates import DuplicateResult
from ap_agent.domain.rules.fraud import FraudIndicator
from ap_agent.domain.rules.matching import MatchResult
from ap_agent.domain.rules.outcome import decide_outcome
from ap_agent.domain.rules.vendor import VendorCheckResult

AS_OF = datetime(2026, 9, 11, tzinfo=UTC)


def _clean_match() -> MatchResult:
    return MatchResult(
        po_present=True,
        receipt_present=True,
        line_level=True,
        all_within_tolerance=True,
        currency_consistent=True,
    )


def _match_with_exception(category: ExceptionCategory, *, blocking: bool = True) -> MatchResult:
    return MatchResult(
        po_present=True,
        receipt_present=False,
        line_level=True,
        all_within_tolerance=not blocking,
        currency_consistent=True,
        exceptions=[
            ExceptionRecord(
                category=category,
                failed_rule="test.rule",
                expected="expected",
                observed="observed",
                owner=EscalationOwner.REQUESTER,
                policy_refs=["FIN-POL-002 §4"],
                blocking=blocking,
            )
        ],
    )


def _clean_duplicates() -> DuplicateResult:
    return DuplicateResult(checked=True)


def _settled_duplicate() -> DuplicateResult:
    record = InvoiceHistoryMatch(
        record_id="AP-2026-11841",
        invoice_reference="INV-2026-0388",
        vendor_id="V-2002",
        currency="AUD",
        gross_amount=Decimal("9240.00"),
        invoice_date=date(2026, 8, 1),
        status=InvoiceHistoryStatus.PAID,
    )
    return DuplicateResult(
        checked=True,
        exact_matches=[record],
        settled_duplicate=record,
        recommended_outcome=Outcome.REJECT_DUPLICATE,
        exceptions=[
            ExceptionRecord(
                category=ExceptionCategory.DUPLICATE_RISK,
                failed_rule="duplicate_check.exact_match_to_settled_record",
                expected="no settled record for this invoice",
                observed="AP-2026-11841 is already PAID",
                owner=EscalationOwner.ACCOUNTS_PAYABLE_MANAGER,
                policy_refs=["FIN-POL-005 §2"],
            )
        ],
    )


def _clean_vendor() -> VendorCheckResult:
    return VendorCheckResult(vendor_present=True, status=VendorStatus.ACTIVE)


def _escalating_vendor() -> VendorCheckResult:
    return VendorCheckResult(
        vendor_present=True,
        status=VendorStatus.SANCTIONS_REVIEW,
        requires_control_escalation=True,
        exceptions=[
            ExceptionRecord(
                category=ExceptionCategory.VENDOR_BLOCK,
                failed_rule="vendor_status_check.status_active",
                expected="ACTIVE",
                observed="SANCTIONS_REVIEW",
                owner=EscalationOwner.VENDOR_GOVERNANCE,
                policy_refs=["FIN-POL-004 §4"],
            )
        ],
    )


def _authority() -> object:
    return required_authority(Money.of("18400.00", "AUD"), higher_risk_reasons=[])


def _indicators(count: int) -> list[FraudIndicator]:
    return [
        FraudIndicator(
            code=f"CODE_{index}",
            description=f"indicator {index}",
            policy_ref="FIN-POL-005 §3",
            source="case_notes",
        )
        for index in range(count)
    ]


class TestCleanPath:
    def test_all_controls_satisfied_approves_for_posting(self) -> None:
        decision = decide_outcome(
            match=_clean_match(),
            duplicates=_clean_duplicates(),
            vendor=_clean_vendor(),
            authority=_authority(),
            indicators=[],
        )
        assert decision.outcome is Outcome.APPROVE_FOR_POSTING
        assert decision.requires_approval

    def test_approval_is_always_required_for_posting(self) -> None:
        """FIN-POL-001 §3: a recommendation is not an approval."""
        decision = decide_outcome(
            match=_clean_match(),
            duplicates=_clean_duplicates(),
            vendor=_clean_vendor(),
            authority=_authority(),
            indicators=[],
        )
        assert decision.requires_approval is True

    def test_reasons_are_recorded_even_on_the_clean_path(self) -> None:
        decision = decide_outcome(
            match=_clean_match(),
            duplicates=_clean_duplicates(),
            vendor=_clean_vendor(),
            authority=_authority(),
            indicators=[],
        )
        assert decision.reasons


class TestHold:
    def test_a_blocking_exception_holds(self) -> None:
        decision = decide_outcome(
            match=_match_with_exception(ExceptionCategory.MISSING_RECEIPT),
            duplicates=_clean_duplicates(),
            vendor=_clean_vendor(),
            authority=_authority(),
            indicators=[],
        )
        assert decision.outcome is Outcome.HOLD_FOR_INFORMATION
        assert not decision.requires_approval

    def test_a_non_blocking_exception_does_not_hold(self) -> None:
        decision = decide_outcome(
            match=_match_with_exception(ExceptionCategory.TAX_QUERY, blocking=False),
            duplicates=_clean_duplicates(),
            vendor=_clean_vendor(),
            authority=_authority(),
            indicators=[],
        )
        assert decision.outcome is Outcome.APPROVE_FOR_POSTING

    def test_a_single_fraud_indicator_is_reported_without_holding_the_invoice(self) -> None:
        """Below the threshold of two, the signal is reported and the case proceeds.

        This test asserted a hold in an earlier revision. A fixture run showed that reading
        was wrong: a clean, fully matched invoice for a round amount was held on that single
        weak signal alone. FIN-POL-005 §3 sets the escalation threshold at two, and §4 states
        that a risk score is decision support only, so a sub-threshold indicator is recorded
        and shown to the approver rather than treated as a control failure. A control that
        stops ordinary invoices on one weak signal is overridden until it is ignored.
        """
        decision = decide_outcome(
            match=_clean_match(),
            duplicates=_clean_duplicates(),
            vendor=_clean_vendor(),
            authority=_authority(),
            indicators=_indicators(1),
        )
        assert decision.outcome is Outcome.APPROVE_FOR_POSTING
        assert decision.indicator_count == 1
        assert any("below the escalation threshold" in reason for reason in decision.reasons), (
            "the indicator must still be visible in the recorded reasons"
        )


class TestEscalation:
    def test_two_indicators_escalate(self) -> None:
        """FIN-POL-005 §3."""
        decision = decide_outcome(
            match=_clean_match(),
            duplicates=_clean_duplicates(),
            vendor=_clean_vendor(),
            authority=_authority(),
            indicators=_indicators(2),
        )
        assert decision.outcome is Outcome.ESCALATE_CONTROL_REVIEW

    def test_sanctions_review_escalates(self) -> None:
        decision = decide_outcome(
            match=_clean_match(),
            duplicates=_clean_duplicates(),
            vendor=_escalating_vendor(),
            authority=_authority(),
            indicators=[],
        )
        assert decision.outcome is Outcome.ESCALATE_CONTROL_REVIEW

    def test_escalation_does_not_require_payment_approval(self) -> None:
        decision = decide_outcome(
            match=_clean_match(),
            duplicates=_clean_duplicates(),
            vendor=_clean_vendor(),
            authority=_authority(),
            indicators=_indicators(3),
        )
        assert not decision.requires_approval
        assert not decision.outcome.proposes_payment


class TestPrecedence:
    def test_escalation_outranks_duplicate_rejection(self) -> None:
        """FIN-POL-007 §3 sends suspected fraud to Financial Crime "without notifying the
        supplier of the suspicion". Rejecting an invoice notifies the supplier, so a case
        with both signals must escalate rather than reject."""
        decision = decide_outcome(
            match=_clean_match(),
            duplicates=_settled_duplicate(),
            vendor=_clean_vendor(),
            authority=_authority(),
            indicators=_indicators(2),
        )
        assert decision.outcome is Outcome.ESCALATE_CONTROL_REVIEW

    def test_duplicate_rejection_outranks_a_hold(self) -> None:
        """A settled duplicate is a conclusion, not a data gap; holding it invites a second
        payment attempt later."""
        decision = decide_outcome(
            match=_match_with_exception(ExceptionCategory.MISSING_RECEIPT),
            duplicates=_settled_duplicate(),
            vendor=_clean_vendor(),
            authority=_authority(),
            indicators=[],
        )
        assert decision.outcome is Outcome.REJECT_DUPLICATE

    def test_invalid_request_outranks_a_hold(self) -> None:
        decision = decide_outcome(
            match=_match_with_exception(ExceptionCategory.MISSING_RECEIPT),
            duplicates=_clean_duplicates(),
            vendor=_clean_vendor(),
            authority=_authority(),
            indicators=[],
            invalid_reasons=["gross amount does not equal net plus tax"],
        )
        assert decision.outcome is Outcome.REJECT_INVALID

    def test_no_path_reaches_approval_when_any_control_is_unsatisfied(self) -> None:
        """The property that matters: approval requires every control to pass."""
        unsatisfied = [
            {"match": _match_with_exception(ExceptionCategory.PRICE_VARIANCE)},
            {"duplicates": _settled_duplicate()},
            {"vendor": _escalating_vendor()},
            {"indicators": _indicators(2)},
            {"invalid_reasons": ["malformed"]},
        ]
        baseline = {
            "match": _clean_match(),
            "duplicates": _clean_duplicates(),
            "vendor": _clean_vendor(),
            "authority": _authority(),
            "indicators": [],
        }
        for override in unsatisfied:
            decision = decide_outcome(**{**baseline, **override})  # type: ignore[arg-type]
            assert decision.outcome is not Outcome.APPROVE_FOR_POSTING, override


class TestSecondApproval:
    def test_higher_risk_requirement_is_carried_into_the_decision(self) -> None:
        authority = required_authority(
            Money.of("18400.00", "AUD"),
            higher_risk_reasons=["bank account changed within the last 30 days"],
        )
        decision = decide_outcome(
            match=_clean_match(),
            duplicates=_clean_duplicates(),
            vendor=_clean_vendor(),
            authority=authority,
            indicators=[],
        )
        assert decision.requires_second_approval
        assert decision.second_approval_reason

    def test_required_role_is_carried_into_the_decision(self) -> None:
        decision = decide_outcome(
            match=_clean_match(),
            duplicates=_clean_duplicates(),
            vendor=_clean_vendor(),
            authority=_authority(),
            indicators=[],
        )
        assert decision.required_role_minimum is ApproverRole.DEPARTMENT_DIRECTOR
