"""Vendor control tests (FIN-POL-004, FIN-POL-001 §4, FIN-POL-003 §3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ap_agent.domain.enums import ExceptionCategory, VendorStatus
from ap_agent.domain.evidence import Invoice, VendorRecord
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
        result = vendor_status_check(_vendor(), _invoice(), as_of=AS_OF)
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

    def test_recent_bank_change_raises_a_bank_change_exception(self) -> None:
        result = vendor_status_check(
            _vendor(bank_changed_at=AS_OF - timedelta(days=2)), _invoice(), as_of=AS_OF
        )
        assert ExceptionCategory.BANK_CHANGE in _categories(result)
        assert result.higher_risk_reasons

    def test_old_bank_change_is_not_flagged(self) -> None:
        result = vendor_status_check(
            _vendor(bank_changed_at=AS_OF - timedelta(days=400)), _invoice(), as_of=AS_OF
        )
        assert ExceptionCategory.BANK_CHANGE not in _categories(result)

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


class TestSegregationOfDuties:
    """FIN-POL-001 §4 and FIN-POL-003 §1."""

    def test_requester_who_created_the_vendor_is_an_exception(self) -> None:
        result = vendor_status_check(
            _vendor(created_by="U-5000"), _invoice(), as_of=AS_OF, requested_by="U-5000"
        )
        assert ExceptionCategory.OTHER_CONTROL_RISK in _categories(result)

    def test_different_requester_and_creator_passes(self) -> None:
        result = vendor_status_check(
            _vendor(created_by="U-1140"), _invoice(), as_of=AS_OF, requested_by="U-5000"
        )
        assert ExceptionCategory.OTHER_CONTROL_RISK not in _categories(result)


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
