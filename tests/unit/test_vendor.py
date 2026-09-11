"""Vendor control tests (FIN-POL-004, FIN-POL-001 §4, FIN-POL-003 §3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ap_agent.domain.enums import ExceptionCategory, InvoiceHistoryStatus, VendorStatus
from ap_agent.domain.evidence import Invoice, InvoiceHistoryMatch, VendorRecord
from ap_agent.domain.rules.vendor import vendor_status_check

AS_OF = datetime(2026, 9, 11, tzinfo=UTC)


def _invoice(*, vendor_name: str = "Brightline Industrial Supplies Pty Ltd") -> Invoice:
    return Invoice(
        invoice_reference="INV-1",
        vendor_id="V-1001",
        vendor_name=vendor_name,
        invoice_date=AS_OF.date(),
        currency="AUD",
        net_amount=Decimal("1000.00"),
        tax_amount=Decimal("0.00"),
        gross_amount=Decimal("1000.00"),
    )


def _vendor(
    *,
    status: VendorStatus = VendorStatus.ACTIVE,
    created_at: datetime | None = None,
    bank_changed_at: datetime | None = None,
    bank_country: str | None = "AU",
    on_payment_hold: bool = False,
    risk_flags: list[str] | None = None,
    created_by: str | None = "U-1140",
) -> VendorRecord:
    return VendorRecord(
        vendor_id="V-1001",
        legal_name="Brightline Industrial Supplies Pty Ltd",
        status=status,
        bank_account_last4="4417",
        bank_country=bank_country,
        bank_details_changed_at=bank_changed_at,
        created_at=created_at or datetime(2019, 6, 14, tzinfo=UTC),
        last_updated_at=AS_OF,
        risk_flags=risk_flags or [],
        on_payment_hold=on_payment_hold,
        created_by=created_by,
    )


def _categories(result: object) -> set[ExceptionCategory]:
    return {exception.category for exception in result.exceptions}  # type: ignore[attr-defined]


class TestVendorStatus:
    def test_active_vendor_passes(self) -> None:
        result = vendor_status_check(_vendor(bank_changed_at=None), _invoice(), as_of=AS_OF)
        assert result.exceptions == []
        assert result.higher_risk_reasons == []
        assert all(finding.satisfied for finding in result.findings)

    @pytest.mark.parametrize(
        "status",
        [
            VendorStatus.BLOCKED,
            VendorStatus.DORMANT,
            VendorStatus.SANCTIONS_REVIEW,
            VendorStatus.PENDING_VERIFICATION,
        ],
    )
    def test_every_non_active_status_is_held(self, status: VendorStatus) -> None:
        """FIN-POL-004 §4: invoices for these statuses must be held."""
        result = vendor_status_check(_vendor(status=status), _invoice(), as_of=AS_OF)
        assert ExceptionCategory.VENDOR_BLOCK in _categories(result)
        assert any(exception.blocking for exception in result.exceptions)

    def test_sanctions_review_is_flagged_for_escalation(self) -> None:
        result = vendor_status_check(
            _vendor(status=VendorStatus.SANCTIONS_REVIEW), _invoice(), as_of=AS_OF
        )
        assert result.requires_control_escalation

    def test_payment_hold_is_reported(self) -> None:
        result = vendor_status_check(_vendor(on_payment_hold=True), _invoice(), as_of=AS_OF)
        assert ExceptionCategory.VENDOR_BLOCK in _categories(result)


class TestHigherRiskTriggers:
    """FIN-POL-003 §3 lists the conditions that require two approvals."""

    def test_new_vendor_under_thirty_days_is_higher_risk(self) -> None:
        result = vendor_status_check(
            _vendor(created_at=AS_OF - timedelta(days=10)), _invoice(), as_of=AS_OF
        )
        assert any(
            "30 days" in reason or "new vendor" in reason.lower()
            for reason in result.higher_risk_reasons
        )

    def test_vendor_over_thirty_days_is_not_flagged_as_new(self) -> None:
        result = vendor_status_check(
            _vendor(created_at=AS_OF - timedelta(days=45)), _invoice(), as_of=AS_OF
        )
        assert not any("new vendor" in reason.lower() for reason in result.higher_risk_reasons)

    def test_a_bank_change_with_no_payment_since_raises_an_exception(self) -> None:
        """FIN-POL-004 §2 attaches co-approval to "the first subsequent payment"."""
        result = vendor_status_check(
            _vendor(bank_changed_at=AS_OF - timedelta(days=2)), _invoice(), as_of=AS_OF
        )
        assert ExceptionCategory.BANK_CHANGE in _categories(result)
        assert result.higher_risk_reasons
        assert result.awaiting_first_payment_after_bank_change

    def test_an_old_bank_change_with_no_payment_since_still_raises(self) -> None:
        """The corrected reading of FIN-POL-004 §2.

        An earlier version applied a 30-day window and called it "more conservative". It was
        the opposite: a controls review pointed out that a vendor whose details changed 45
        days ago, with no payment since, lost the co-approval requirement entirely. The
        policy attaches it to the first subsequent payment, with no time limit.
        """
        result = vendor_status_check(
            _vendor(bank_changed_at=AS_OF - timedelta(days=400)), _invoice(), as_of=AS_OF
        )
        assert ExceptionCategory.BANK_CHANGE in _categories(result)
        assert result.awaiting_first_payment_after_bank_change

    def test_a_settled_payment_after_the_change_discharges_the_requirement(self) -> None:
        paid_after = InvoiceHistoryMatch(
            record_id="AP-2026-99001",
            invoice_reference="INV-2026-0099",
            vendor_id="V-1001",
            currency="AUD",
            gross_amount=Decimal("500.00"),
            invoice_date=(AS_OF - timedelta(days=1)).date(),
            status=InvoiceHistoryStatus.PAID,
        )
        result = vendor_status_check(
            _vendor(bank_changed_at=AS_OF - timedelta(days=10)),
            _invoice(),
            as_of=AS_OF,
            history=[paid_after],
        )
        assert ExceptionCategory.BANK_CHANGE not in _categories(result)
        assert not result.awaiting_first_payment_after_bank_change

    def test_a_payment_before_the_change_does_not_discharge_it(self) -> None:
        paid_before = InvoiceHistoryMatch(
            record_id="AP-2026-99002",
            invoice_reference="INV-2026-0098",
            vendor_id="V-1001",
            currency="AUD",
            gross_amount=Decimal("500.00"),
            invoice_date=(AS_OF - timedelta(days=60)).date(),
            status=InvoiceHistoryStatus.PAID,
        )
        result = vendor_status_check(
            _vendor(bank_changed_at=AS_OF - timedelta(days=10)),
            _invoice(),
            as_of=AS_OF,
            history=[paid_before],
        )
        assert ExceptionCategory.BANK_CHANGE in _categories(result)

    def test_a_held_record_after_the_change_does_not_discharge_it(self) -> None:
        """Only a settled payment counts: a hold is not a payment."""
        held_after = InvoiceHistoryMatch(
            record_id="AP-2026-99003",
            invoice_reference="INV-2026-0097",
            vendor_id="V-1001",
            currency="AUD",
            gross_amount=Decimal("500.00"),
            invoice_date=(AS_OF - timedelta(days=1)).date(),
            status=InvoiceHistoryStatus.HELD,
        )
        result = vendor_status_check(
            _vendor(bank_changed_at=AS_OF - timedelta(days=10)),
            _invoice(),
            as_of=AS_OF,
            history=[held_after],
        )
        assert ExceptionCategory.BANK_CHANGE in _categories(result)

    def test_overseas_account_is_higher_risk(self) -> None:
        result = vendor_status_check(_vendor(bank_country="GB"), _invoice(), as_of=AS_OF)
        assert any("overseas" in reason.lower() for reason in result.higher_risk_reasons)

    def test_unknown_bank_country_is_treated_as_overseas(self) -> None:
        """The conservative default: missing reference data must not remove a control."""
        result = vendor_status_check(_vendor(bank_country=None), _invoice(), as_of=AS_OF)
        assert any("overseas" in reason.lower() for reason in result.higher_risk_reasons)

    def test_risk_flags_are_surfaced(self) -> None:
        result = vendor_status_check(_vendor(risk_flags=["FRAUD_WATCH"]), _invoice(), as_of=AS_OF)
        assert any("FRAUD_WATCH" in reason for reason in result.higher_risk_reasons)
        assert result.requires_control_escalation


class TestSegregationIsNotEvaluatedHere:
    """FIN-POL-001 §4 constrains the *approver*, so it cannot be evaluated at reconciliation.

    An earlier version compared the requester against the vendor creator and, when they
    differed, recorded §4 as satisfied. A controls review pointed out that this tested a
    relationship the policy does not constrain and reported a section as satisfied whose
    actual requirements had never been evaluated. The checks now live in
    ``domain/rules/segregation.py`` and are exercised by ``test_segregation.py``.
    """

    def test_no_segregation_finding_is_claimed_here(self) -> None:
        result = vendor_status_check(
            _vendor(created_by="U-1140"), _invoice(), as_of=AS_OF, requested_by="U-5000"
        )
        assert not any(finding.rule.startswith("segregation") for finding in result.findings), (
            "the vendor check must not claim a control it does not evaluate"
        )


class TestNameAgreement:
    def test_mismatched_vendor_name_is_recorded(self) -> None:
        result = vendor_status_check(
            _vendor(), _invoice(vendor_name="Brightline Industrial Supplies Limited"), as_of=AS_OF
        )
        assert result.name_mismatch

    def test_case_and_whitespace_differences_are_not_a_mismatch(self) -> None:
        result = vendor_status_check(
            _vendor(),
            _invoice(vendor_name="  brightline industrial supplies pty ltd "),
            as_of=AS_OF,
        )
        assert not result.name_mismatch


class TestMissingVendor:
    def test_absent_vendor_record_is_an_unknown_and_a_blocking_exception(self) -> None:
        result = vendor_status_check(None, _invoice(), as_of=AS_OF)
        assert not result.vendor_present
        assert result.unknowns
        assert any(exception.blocking for exception in result.exceptions)

    def test_absent_vendor_does_not_claim_a_status(self) -> None:
        result = vendor_status_check(None, _invoice(), as_of=AS_OF)
        assert result.status is None
