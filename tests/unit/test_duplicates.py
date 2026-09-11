"""Duplicate detection tests (FIN-POL-005 §1 and §2)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from ap_agent.domain.enums import ExceptionCategory, InvoiceHistoryStatus, Outcome
from ap_agent.domain.evidence import Invoice, InvoiceHistoryMatch
from ap_agent.domain.rules.duplicates import duplicate_check

AS_OF = datetime(2026, 9, 11, tzinfo=UTC)
TODAY = AS_OF.date()


def _invoice(
    *,
    reference: str = "INV-2026-0388",
    gross: str = "9240.00",
    currency: str = "AUD",
    invoice_date: date | None = None,
    po_reference: str | None = "PO-77001",
    hashes: list[str] | None = None,
) -> Invoice:
    return Invoice(
        invoice_reference=reference,
        vendor_id="V-2002",
        vendor_name="Kestrel Facilities Group",
        invoice_date=invoice_date or TODAY,
        currency=currency,
        net_amount=Decimal(gross),
        tax_amount=Decimal("0.00"),
        gross_amount=Decimal(gross),
        po_reference=po_reference,
        attachment_hashes=hashes or [],
    )


def _record(
    *,
    record_id: str = "AP-2026-11841",
    reference: str = "INV-2026-0388",
    gross: str = "9240.00",
    currency: str = "AUD",
    status: InvoiceHistoryStatus = InvoiceHistoryStatus.PAID,
    invoice_date: date | None = None,
    po_reference: str | None = "PO-77001",
    hashes: list[str] | None = None,
) -> InvoiceHistoryMatch:
    return InvoiceHistoryMatch(
        record_id=record_id,
        invoice_reference=reference,
        vendor_id="V-2002",
        currency=currency,
        gross_amount=Decimal(gross),
        invoice_date=invoice_date or TODAY,
        status=status,
        po_reference=po_reference,
        attachment_hashes=hashes or [],
    )


def _categories(result: object) -> set[ExceptionCategory]:
    return {exception.category for exception in result.exceptions}  # type: ignore[attr-defined]


class TestExactMatching:
    """FIN-POL-005 §1: vendor ID, normalised invoice number, currency and gross amount."""

    def test_identical_paid_record_is_an_exact_settled_duplicate(self) -> None:
        result = duplicate_check(_invoice(), [_record()], as_of=AS_OF)
        assert result.settled_duplicate is not None
        assert result.settled_duplicate.record_id == "AP-2026-11841"
        assert result.recommended_outcome is Outcome.REJECT_DUPLICATE

    def test_punctuation_differences_still_match_exactly(self) -> None:
        result = duplicate_check(_invoice(reference="inv/2026 0388"), [_record()], as_of=AS_OF)
        assert result.exact_matches

    def test_different_amount_is_not_an_exact_match(self) -> None:
        result = duplicate_check(_invoice(), [_record(gross="9241.00")], as_of=AS_OF)
        assert not result.exact_matches

    def test_different_currency_is_not_an_exact_match(self) -> None:
        result = duplicate_check(_invoice(), [_record(currency="USD")], as_of=AS_OF)
        assert not result.exact_matches

    def test_different_vendor_is_not_a_match(self) -> None:
        record = _record().model_copy(update={"vendor_id": "V-9999"})
        result = duplicate_check(_invoice(), [record], as_of=AS_OF)
        assert not result.exact_matches
        assert not result.fuzzy_matches


class TestSettledVersusUnsettled:
    """FIN-POL-005 §2: only paid or posted records make a new invoice a duplicate."""

    def test_posted_record_is_settled(self) -> None:
        result = duplicate_check(
            _invoice(), [_record(status=InvoiceHistoryStatus.POSTED)], as_of=AS_OF
        )
        assert result.settled_duplicate is not None

    def test_prior_rejection_does_not_prove_a_duplicate(self) -> None:
        """ "A prior rejection does not automatically prove a new invoice is a duplicate"."""
        result = duplicate_check(
            _invoice(), [_record(status=InvoiceHistoryStatus.REJECTED)], as_of=AS_OF
        )
        assert result.settled_duplicate is None
        assert result.recommended_outcome is Outcome.HOLD_FOR_INFORMATION

    def test_prior_hold_is_reported_but_not_settled(self) -> None:
        result = duplicate_check(
            _invoice(), [_record(status=InvoiceHistoryStatus.HELD)], as_of=AS_OF
        )
        assert result.settled_duplicate is None
        assert result.exact_matches


class TestFuzzyMatching:
    """FIN-POL-005 §1 fuzzy signals."""

    def test_near_amount_within_the_date_window_is_a_probable_match(self) -> None:
        record = _record(
            record_id="AP-2026-11900",
            reference="INV-2026-0389",
            gross="9260.00",  # 0.216% above, inside 0.5%
            invoice_date=TODAY - timedelta(days=10),
        )
        result = duplicate_check(_invoice(), [record], as_of=AS_OF)
        assert result.fuzzy_matches
        assert result.recommended_outcome is Outcome.HOLD_FOR_INFORMATION

    def test_amount_variance_at_exactly_half_a_percent_is_not_a_fuzzy_match(self) -> None:
        """Policy says "amount variance below 0.5%", so 0.5% itself does not qualify.

        The variance is expressed as a percentage of the **prior record's** amount, which is
        the settled baseline. FIN-POL-005 §1 does not name the denominator; using the prior
        amount is the standard variance convention and is the only stable choice, since the
        candidate amount is the value under assessment. Here 50.00 against a prior 10,000.00
        is exactly 0.5%.
        """
        record = _record(
            record_id="AP-2026-11901",
            reference="INV-2026-0390",
            gross="10000.00",
            invoice_date=TODAY - timedelta(days=3),
            po_reference=None,
        )
        result = duplicate_check(
            _invoice(gross="10050.00", po_reference=None), [record], as_of=AS_OF
        )
        assert not result.fuzzy_matches

    def test_amount_variance_just_below_half_a_percent_is_a_fuzzy_match(self) -> None:
        """The companion to the boundary case above: 49.00 on 10,000.00 is 0.49%."""
        record = _record(
            record_id="AP-2026-11907",
            reference="INV-2026-0395",
            gross="10000.00",
            invoice_date=TODAY - timedelta(days=3),
            po_reference=None,
        )
        result = duplicate_check(
            _invoice(gross="10049.00", po_reference=None), [record], as_of=AS_OF
        )
        assert result.fuzzy_matches

    def test_outside_the_fourteen_day_window_is_not_a_fuzzy_match_on_date_alone(self) -> None:
        record = _record(
            record_id="AP-2026-11902",
            reference="INV-2026-0391",
            gross="9250.00",
            invoice_date=TODAY - timedelta(days=15),
            po_reference=None,
        )
        result = duplicate_check(_invoice(po_reference=None), [record], as_of=AS_OF)
        assert not result.fuzzy_matches

    def test_identical_attachment_hash_alone_is_a_probable_match(self) -> None:
        """An identical document is strong evidence regardless of the other fields."""
        record = _record(
            record_id="AP-2026-11903",
            reference="INV-2026-0500",
            gross="15000.00",
            invoice_date=TODAY - timedelta(days=200),
            po_reference=None,
            hashes=["a" * 64],
        )
        result = duplicate_check(
            _invoice(po_reference=None, hashes=["a" * 64]), [record], as_of=AS_OF
        )
        assert result.fuzzy_matches

    def test_same_purchase_order_with_a_near_amount_is_a_probable_match(self) -> None:
        record = _record(
            record_id="AP-2026-11904",
            reference="INV-2026-0392",
            gross="9250.00",
            invoice_date=TODAY - timedelta(days=90),
        )
        result = duplicate_check(_invoice(), [record], as_of=AS_OF)
        assert result.fuzzy_matches

    def test_fuzzy_match_reasons_are_recorded(self) -> None:
        record = _record(
            record_id="AP-2026-11905",
            reference="INV-2026-0393",
            gross="9260.00",
            invoice_date=TODAY - timedelta(days=5),
        )
        result = duplicate_check(_invoice(), [record], as_of=AS_OF)
        assert result.fuzzy_matches[0].match_reasons


class TestCleanHistory:
    def test_no_history_produces_a_satisfied_finding_and_no_exception(self) -> None:
        result = duplicate_check(_invoice(), [], as_of=AS_OF)
        assert result.exceptions == []
        assert result.findings
        assert all(finding.satisfied for finding in result.findings)
        assert result.recommended_outcome is None

    def test_unrelated_history_is_ignored(self) -> None:
        record = _record(
            record_id="AP-2026-12000",
            reference="INV-2025-0001",
            gross="120.00",
            invoice_date=TODAY - timedelta(days=400),
            po_reference="PO-00001",
        )
        result = duplicate_check(_invoice(), [record], as_of=AS_OF)
        assert result.exceptions == []


class TestExceptionQuality:
    def test_exception_cites_both_record_identifiers(self) -> None:
        """FIN-POL-005 §2 requires both record IDs to be cited."""
        result = duplicate_check(_invoice(), [_record()], as_of=AS_OF)
        exception = next(
            e for e in result.exceptions if e.category is ExceptionCategory.DUPLICATE_RISK
        )
        assert "AP-2026-11841" in exception.observed
        assert "INV-2026-0388" in exception.expected or "INV-2026-0388" in exception.observed
        assert any("FIN-POL-005" in ref for ref in exception.policy_refs)

    def test_duplicate_exception_is_blocking(self) -> None:
        result = duplicate_check(_invoice(), [_record()], as_of=AS_OF)
        assert all(
            e.blocking for e in result.exceptions if e.category is ExceptionCategory.DUPLICATE_RISK
        )

    def test_calculations_record_the_amount_comparison(self) -> None:
        record = _record(
            record_id="AP-2026-11906",
            reference="INV-2026-0394",
            gross="9260.00",
            invoice_date=TODAY - timedelta(days=5),
        )
        result = duplicate_check(_invoice(), [record], as_of=AS_OF)
        assert any("variance" in calculation.name for calculation in result.calculations)


class TestReusedInvoiceNumber:
    """FIN-POL-005 §1 names punctuation-stripped invoice numbers as a fuzzy signal.

    The gap these cover: the number comparison previously existed only inside the exact test,
    which also requires the currency and gross amount to agree. A resubmission that reused
    the number but changed the amount therefore matched neither test, and the amount change is
    exactly what a supplier correcting and resubmitting an invoice would do.
    """

    def test_the_same_number_with_a_different_amount_is_a_fuzzy_match(self) -> None:
        result = duplicate_check(
            _invoice(reference="INV 2026 0388", gross="9900.00"),
            [_record(gross="9240.00")],
            as_of=AS_OF,
        )
        assert result.fuzzy_matches, "a reused invoice number must not pass unremarked"
        assert result.recommended_outcome is Outcome.HOLD_FOR_INFORMATION

    def test_the_reason_names_the_normalised_number(self) -> None:
        """FIN-POL-007 §2 rejects generic notes; the record must say which number repeated."""
        result = duplicate_check(
            _invoice(reference="inv/2026-0388", gross="9900.00"),
            [_record(gross="9240.00")],
            as_of=AS_OF,
        )
        assert "INV20260388" in " ".join(result.fuzzy_matches[0].match_reasons)

    def test_a_different_number_and_a_distant_amount_is_not_a_match(self) -> None:
        """The signal must be the number, not the vendor: recurring invoices are normal."""
        result = duplicate_check(
            _invoice(reference="INV-2026-0999", gross="15000.00"),
            [_record(gross="9240.00")],
            as_of=AS_OF,
        )
        assert not result.has_any_match

    def test_a_reused_number_on_a_settled_record_still_rejects_when_exact(self) -> None:
        """The new signal must not demote an exact settled match to a hold."""
        result = duplicate_check(_invoice(), [_record()], as_of=AS_OF)
        assert result.recommended_outcome is Outcome.REJECT_DUPLICATE
