"""Repeated non-PO purchasing (FIN-POL-012 §4).

The gap these cover: §4 requires escalation when one supplier sends two or more invoices with
no purchase order inside 90 days, and no code path implemented it. It is the only control in
the corpus about a pattern rather than a transaction, so per-invoice checks could not have
caught it: neither invoice is wrong on its own.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from ap_agent.domain.enums import InvoiceHistoryStatus
from ap_agent.domain.evidence import Invoice, InvoiceHistoryMatch
from ap_agent.domain.rules.non_po import REPEAT_WINDOW_DAYS, check_repeated_non_po

AS_OF = datetime(2026, 9, 11, tzinfo=UTC)
INVOICE_DATE = date(2026, 9, 9)


def _invoice(*, po_reference: str | None = None, vendor_id: str = "V-3003") -> Invoice:
    return Invoice(
        invoice_reference="INV-2026-0701",
        vendor_id=vendor_id,
        vendor_name="Harbour Utilities Ltd",
        invoice_date=INVOICE_DATE,
        currency="AUD",
        net_amount=Decimal("4200.00"),
        tax_amount=Decimal("420.00"),
        gross_amount=Decimal("4620.00"),
        po_reference=po_reference,
    )


def _record(
    *,
    record_id: str = "AP-2026-20001",
    days_before: int = 30,
    po_reference: str | None = None,
    vendor_id: str = "V-3003",
) -> InvoiceHistoryMatch:
    return InvoiceHistoryMatch(
        record_id=record_id,
        invoice_reference=f"INV-2026-{record_id[-4:]}",
        vendor_id=vendor_id,
        currency="AUD",
        gross_amount=Decimal("4620.00"),
        invoice_date=INVOICE_DATE - timedelta(days=days_before),
        status=InvoiceHistoryStatus.PAID,
        po_reference=po_reference,
    )


class TestRepeatedNonPo:
    def test_a_second_non_po_invoice_inside_the_window_is_escalated(self) -> None:
        result = check_repeated_non_po(_invoice(), [_record()], as_of=AS_OF)
        assert result.exceptions
        assert result.prior_non_po_records == ["AP-2026-20001"]

    def test_a_single_non_po_invoice_is_not(self) -> None:
        """§1 permits non-PO processing for utilities, leases and statutory charges."""
        result = check_repeated_non_po(_invoice(), [], as_of=AS_OF)
        assert not result.exceptions
        finding = next(f for f in result.findings if f.rule == "non_po_use_not_repeated")
        assert finding.satisfied

    def test_a_po_backed_invoice_is_outside_the_control(self) -> None:
        """Recording a satisfied finding on every ordinary invoice would bury the real ones."""
        result = check_repeated_non_po(_invoice(po_reference="PO-88121"), [_record()], as_of=AS_OF)
        assert not result.assessed
        assert not result.findings

    def test_a_prior_record_that_had_a_purchase_order_does_not_count(self) -> None:
        result = check_repeated_non_po(_invoice(), [_record(po_reference="PO-88121")], as_of=AS_OF)
        assert not result.exceptions

    def test_a_prior_record_outside_the_window_does_not_count(self) -> None:
        result = check_repeated_non_po(
            _invoice(), [_record(days_before=REPEAT_WINDOW_DAYS + 1)], as_of=AS_OF
        )
        assert not result.exceptions

    def test_the_window_boundary_is_inclusive(self) -> None:
        result = check_repeated_non_po(
            _invoice(), [_record(days_before=REPEAT_WINDOW_DAYS)], as_of=AS_OF
        )
        assert result.exceptions

    def test_another_vendor_does_not_count(self) -> None:
        """§4 is about one supplier's pattern, not the volume of non-PO spend generally."""
        result = check_repeated_non_po(_invoice(), [_record(vendor_id="V-9999")], as_of=AS_OF)
        assert not result.exceptions

    def test_it_escalates_without_holding_the_invoice(self) -> None:
        """A sourcing review concerns the relationship; the goods were still received.

        Blocking would force HOLD_FOR_INFORMATION and penalise the supplier for a gap on the
        buying side.
        """
        result = check_repeated_non_po(_invoice(), [_record()], as_of=AS_OF)
        assert not any(exception.blocking for exception in result.exceptions)

    def test_it_goes_to_the_accounts_payable_manager(self) -> None:
        result = check_repeated_non_po(_invoice(), [_record()], as_of=AS_OF)
        assert result.exceptions[0].owner.value == "ACCOUNTS_PAYABLE_MANAGER"

    def test_the_exception_names_the_prior_cases(self) -> None:
        """§4's second sentence: prior cases are retrieved before an exception is recommended."""
        result = check_repeated_non_po(
            _invoice(),
            [_record(record_id="AP-2026-20001"), _record(record_id="AP-2026-20002")],
            as_of=AS_OF,
        )
        assert "AP-2026-20001" in result.exceptions[0].observed
        assert "AP-2026-20002" in result.exceptions[0].observed
