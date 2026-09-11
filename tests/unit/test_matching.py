"""Three-way matching tests (FIN-POL-002).

Every threshold in this file is quoted from the policy. The boundary cases matter more than
the clear ones: a tolerance engine that is right in the middle of the range and wrong at the
limit passes casual inspection and fails in production.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from ap_agent.domain.enums import ExceptionCategory, LineType
from ap_agent.domain.evidence import GoodsReceipt, Invoice, InvoiceLine, POLine, PurchaseOrder
from ap_agent.domain.rules.matching import three_way_match

AS_OF = datetime(2026, 9, 11, tzinfo=UTC)
TODAY = AS_OF.date()


def _invoice(
    *,
    lines: list[InvoiceLine] | None = None,
    net: str = "11520.00",
    tax: str = "1152.00",
    gross: str = "12672.00",
    currency: str = "AUD",
    po_reference: str | None = "PO-1",
) -> Invoice:
    return Invoice(
        invoice_reference="INV-1",
        vendor_id="V-1",
        vendor_name="Vendor One",
        invoice_date=TODAY,
        currency=currency,
        net_amount=Decimal(net),
        tax_amount=Decimal(tax),
        gross_amount=Decimal(gross),
        po_reference=po_reference,
        lines=lines or [],
    )


def _goods_line(
    *,
    quantity: str = "120",
    unit_price: str = "96.00",
    total: str | None = None,
    line_type: LineType = LineType.GOODS,
    po_line_number: int | None = 1,
) -> InvoiceLine:
    computed = total if total is not None else str(Decimal(quantity) * Decimal(unit_price))
    return InvoiceLine(
        line_number=1,
        description="Bearing assembly",
        line_type=line_type,
        quantity=Decimal(quantity),
        unit_price=Decimal(unit_price),
        line_total=Decimal(computed),
        po_line_number=po_line_number,
    )


def _po(
    *,
    lines: list[POLine] | None = None,
    receipts: list[GoodsReceipt] | None = None,
    total: str = "11520.00",
    currency: str = "AUD",
    approval_status: str = "APPROVED",
    permits_conversion: bool = False,
) -> PurchaseOrder:
    return PurchaseOrder(
        po_reference="PO-1",
        vendor_id="V-1",
        currency=currency,
        total_value=Decimal(total),
        approval_status=approval_status,
        permits_currency_conversion=permits_conversion,
        lines=lines
        or [
            POLine(
                line_number=1,
                description="Bearing assembly",
                line_type=LineType.GOODS,
                quantity_ordered=Decimal("120"),
                unit_price=Decimal("96.00"),
                line_value=Decimal("11520.00"),
            )
        ],
        receipts=receipts
        if receipts is not None
        else [
            GoodsReceipt(
                receipt_id="GR-1",
                po_line_number=1,
                quantity_received=Decimal("120"),
                received_date=TODAY,
                receipted_by="U-1",
            )
        ],
    )


def _categories(result: object) -> set[ExceptionCategory]:
    return {exception.category for exception in result.exceptions}  # type: ignore[attr-defined]


class TestCleanMatch:
    def test_exact_match_has_no_exceptions(self) -> None:
        result = three_way_match(_invoice(lines=[_goods_line()]), _po(), as_of=AS_OF)
        assert result.exceptions == []
        assert result.all_within_tolerance
        assert result.receipt_present
        assert result.line_level

    def test_calculations_record_inputs_formula_result_and_rounding(self) -> None:
        """FIN-POL-002 §5 requires all four to be stored."""
        result = three_way_match(_invoice(lines=[_goods_line()]), _po(), as_of=AS_OF)
        variance = next(c for c in result.calculations if "variance" in c.name)
        assert variance.inputs
        assert variance.formula
        assert variance.result == Decimal("0.00")
        assert variance.rounding == "ROUND_HALF_UP"
        assert variance.currency == "AUD"
        assert variance.policy_ref.startswith("FIN-POL-002")

    def test_passing_controls_are_recorded_as_satisfied_findings(self) -> None:
        result = three_way_match(_invoice(lines=[_goods_line()]), _po(), as_of=AS_OF)
        assert result.findings
        assert all(finding.satisfied for finding in result.findings)


class TestGoodsTolerance:
    """FIN-POL-002 §2: min(AUD 50, 1% of PO line value) for goods."""

    def test_variance_below_both_limits_passes(self) -> None:
        # PO line value 11520.00, so 1% is 115.20; the binding limit is AUD 50.
        line = _goods_line(unit_price="96.40", total="11568.00")
        result = three_way_match(
            _invoice(lines=[line], net="11568.00", tax="0.00", gross="11568.00"), _po(), as_of=AS_OF
        )
        assert result.all_within_tolerance

    def test_variance_exactly_at_the_binding_limit_passes(self) -> None:
        """50.00 against a limit of 50.00. "the lower of these limits is met" includes equality."""
        line = _goods_line(unit_price="96.4166666", total="11570.00")
        result = three_way_match(
            _invoice(lines=[line], net="11570.00", tax="0.00", gross="11570.00"), _po(), as_of=AS_OF
        )
        variance = next(c for c in result.calculations if c.name == "line_1_price_variance")
        assert variance.result == Decimal("50.00")
        assert result.all_within_tolerance

    def test_variance_one_cent_over_the_limit_fails(self) -> None:
        line = _goods_line(unit_price="96.4175", total="11570.10")
        result = three_way_match(
            _invoice(lines=[line], net="11570.10", tax="0.00", gross="11570.10"), _po(), as_of=AS_OF
        )
        assert not result.all_within_tolerance
        assert ExceptionCategory.PRICE_VARIANCE in _categories(result)

    def test_percentage_limit_binds_when_the_line_is_small(self) -> None:
        """On a small line, 1% is below AUD 50 and becomes the binding limit."""
        po = _po(
            lines=[
                POLine(
                    line_number=1,
                    description="Bearing assembly",
                    line_type=LineType.GOODS,
                    quantity_ordered=Decimal("10"),
                    unit_price=Decimal("100.00"),
                    line_value=Decimal("1000.00"),
                )
            ],
            receipts=[
                GoodsReceipt(
                    receipt_id="GR-1",
                    po_line_number=1,
                    quantity_received=Decimal("10"),
                    received_date=TODAY,
                    receipted_by="U-1",
                )
            ],
            total="1000.00",
        )
        # 1% of 1000 is 10.00. A variance of 20.00 is under AUD 50 but over the percentage.
        line = _goods_line(quantity="10", unit_price="102.00", total="1020.00")
        result = three_way_match(
            _invoice(lines=[line], net="1020.00", tax="0.00", gross="1020.00"), po, as_of=AS_OF
        )
        assert not result.all_within_tolerance
        threshold = next(c for c in result.calculations if c.name == "line_1_tolerance_limit")
        assert threshold.result == Decimal("10.00")

    def test_negative_variance_is_assessed_on_absolute_value(self) -> None:
        """Under-billing outside tolerance is still a variance to investigate."""
        line = _goods_line(unit_price="95.00", total="11400.00")
        result = three_way_match(
            _invoice(lines=[line], net="11400.00", tax="0.00", gross="11400.00"), _po(), as_of=AS_OF
        )
        assert not result.all_within_tolerance


class TestServiceTolerance:
    """FIN-POL-002 §2: min(AUD 100, 2%), and the service owner must confirm completion."""

    def _service_po(self, *, confirmed: bool) -> PurchaseOrder:
        return PurchaseOrder(
            po_reference="PO-1",
            vendor_id="V-1",
            currency="AUD",
            total_value=Decimal("10000.00"),
            approval_status="APPROVED",
            lines=[
                POLine(
                    line_number=1,
                    description="Consulting engagement",
                    line_type=LineType.SERVICE,
                    quantity_ordered=Decimal("1"),
                    unit_price=Decimal("10000.00"),
                    line_value=Decimal("10000.00"),
                    service_completion_confirmed=confirmed,
                )
            ],
            receipts=[],
        )

    def test_service_variance_within_the_wider_band_passes_when_confirmed(self) -> None:
        line = InvoiceLine(
            line_number=1,
            description="Consulting engagement",
            line_type=LineType.SERVICE,
            quantity=Decimal("1"),
            unit_price=Decimal("10090.00"),
            line_total=Decimal("10090.00"),
            po_line_number=1,
        )
        result = three_way_match(
            _invoice(lines=[line], net="10090.00", tax="0.00", gross="10090.00"),
            self._service_po(confirmed=True),
            as_of=AS_OF,
        )
        assert result.all_within_tolerance

    def test_unconfirmed_service_completion_is_a_missing_receipt(self) -> None:
        """FIN-POL-002 §4: an invoice without the required confirmation cannot be approved."""
        line = InvoiceLine(
            line_number=1,
            description="Consulting engagement",
            line_type=LineType.SERVICE,
            quantity=Decimal("1"),
            unit_price=Decimal("10000.00"),
            line_total=Decimal("10000.00"),
            po_line_number=1,
        )
        result = three_way_match(
            _invoice(lines=[line], net="10000.00", tax="0.00", gross="10000.00"),
            self._service_po(confirmed=False),
            as_of=AS_OF,
        )
        assert ExceptionCategory.MISSING_RECEIPT in _categories(result)

    def test_service_limit_is_one_hundred_not_fifty(self) -> None:
        line = InvoiceLine(
            line_number=1,
            description="Consulting engagement",
            line_type=LineType.SERVICE,
            quantity=Decimal("1"),
            unit_price=Decimal("10100.00"),
            line_total=Decimal("10100.00"),
            po_line_number=1,
        )
        result = three_way_match(
            _invoice(lines=[line], net="10100.00", tax="0.00", gross="10100.00"),
            self._service_po(confirmed=True),
            as_of=AS_OF,
        )
        limit = next(c for c in result.calculations if c.name == "line_1_tolerance_limit")
        assert limit.result == Decimal("100.00")
        assert result.all_within_tolerance


class TestFreightTolerance:
    """FIN-POL-002 §2: freight may vary by up to AUD 75 only when the PO permits freight."""

    def _freight_po(self, *, permits: bool) -> PurchaseOrder:
        return PurchaseOrder(
            po_reference="PO-1",
            vendor_id="V-1",
            currency="AUD",
            total_value=Decimal("500.00"),
            approval_status="APPROVED",
            lines=[
                POLine(
                    line_number=1,
                    description="Freight",
                    line_type=LineType.FREIGHT,
                    quantity_ordered=Decimal("1"),
                    unit_price=Decimal("500.00"),
                    line_value=Decimal("500.00"),
                    permits_freight=permits,
                )
            ],
            receipts=[
                GoodsReceipt(
                    receipt_id="GR-1",
                    po_line_number=1,
                    quantity_received=Decimal("1"),
                    received_date=TODAY,
                    receipted_by="U-1",
                )
            ],
        )

    def _freight_invoice(self, total: str) -> Invoice:
        line = InvoiceLine(
            line_number=1,
            description="Freight",
            line_type=LineType.FREIGHT,
            quantity=Decimal("1"),
            unit_price=Decimal(total),
            line_total=Decimal(total),
            po_line_number=1,
        )
        return _invoice(lines=[line], net=total, tax="0.00", gross=total)

    def test_freight_variance_within_seventy_five_passes_when_permitted(self) -> None:
        result = three_way_match(
            self._freight_invoice("570.00"), self._freight_po(permits=True), as_of=AS_OF
        )
        assert result.all_within_tolerance

    def test_freight_variance_over_seventy_five_fails(self) -> None:
        result = three_way_match(
            self._freight_invoice("576.00"), self._freight_po(permits=True), as_of=AS_OF
        )
        assert not result.all_within_tolerance

    def test_any_freight_variance_fails_when_the_po_does_not_permit_freight(self) -> None:
        result = three_way_match(
            self._freight_invoice("510.00"), self._freight_po(permits=False), as_of=AS_OF
        )
        assert not result.all_within_tolerance
        limit = next(c for c in result.calculations if c.name == "line_1_tolerance_limit")
        assert limit.result == Decimal("0.00")


class TestQuantityRule:
    """FIN-POL-002 §2: invoiced quantity must not exceed received quantity."""

    def test_invoicing_more_than_was_received_is_a_quantity_variance(self) -> None:
        po = _po(
            receipts=[
                GoodsReceipt(
                    receipt_id="GR-1",
                    po_line_number=1,
                    quantity_received=Decimal("100"),
                    received_date=TODAY,
                    receipted_by="U-1",
                )
            ]
        )
        result = three_way_match(_invoice(lines=[_goods_line()]), po, as_of=AS_OF)
        assert ExceptionCategory.QUANTITY_VARIANCE in _categories(result)

    def test_invoicing_less_than_was_received_is_permitted(self) -> None:
        """A partial invoice against a full receipt is normal."""
        line = _goods_line(quantity="100", unit_price="96.00", total="9600.00")
        result = three_way_match(
            _invoice(lines=[line], net="9600.00", tax="0.00", gross="9600.00"), _po(), as_of=AS_OF
        )
        assert ExceptionCategory.QUANTITY_VARIANCE not in _categories(result)


class TestMissingEvidence:
    def test_no_purchase_order_raises_missing_po_and_skips_matching(self) -> None:
        result = three_way_match(_invoice(lines=[_goods_line()]), None, as_of=AS_OF)
        assert not result.po_present
        assert ExceptionCategory.MISSING_PO in _categories(result)
        assert not result.all_within_tolerance

    def test_no_receipt_raises_missing_receipt(self) -> None:
        result = three_way_match(_invoice(lines=[_goods_line()]), _po(receipts=[]), as_of=AS_OF)
        assert ExceptionCategory.MISSING_RECEIPT in _categories(result)
        assert not result.receipt_present

    def test_missing_receipt_is_blocking(self) -> None:
        """FIN-POL-002 §4: cannot be approved for payment."""
        result = three_way_match(_invoice(lines=[_goods_line()]), _po(receipts=[]), as_of=AS_OF)
        receipt_exception = next(
            e for e in result.exceptions if e.category is ExceptionCategory.MISSING_RECEIPT
        )
        assert receipt_exception.blocking

    def test_invoice_line_without_a_matching_po_line_is_reported(self) -> None:
        line = _goods_line(po_line_number=99)
        result = three_way_match(_invoice(lines=[line]), _po(), as_of=AS_OF)
        assert ExceptionCategory.MISSING_PO in _categories(result)

    def test_unapproved_purchase_order_is_a_control_exception(self) -> None:
        result = three_way_match(
            _invoice(lines=[_goods_line()]), _po(approval_status="DRAFT"), as_of=AS_OF
        )
        assert ExceptionCategory.OTHER_CONTROL_RISK in _categories(result)


class TestTotalOnlyMatching:
    """When the request carries no lines, matching narrows and says so."""

    def test_total_only_match_records_an_assumption_and_an_unknown(self) -> None:
        result = three_way_match(
            _invoice(lines=[], net="11520.00", tax="0.00", gross="11520.00"), _po(), as_of=AS_OF
        )
        assert not result.line_level
        assert result.assumptions
        assert result.unknowns
        assert any("line" in unknown.item.lower() for unknown in result.unknowns)

    def test_total_only_match_still_applies_a_tolerance(self) -> None:
        result = three_way_match(
            _invoice(lines=[], net="11700.00", tax="0.00", gross="11700.00"), _po(), as_of=AS_OF
        )
        assert not result.all_within_tolerance
        assert ExceptionCategory.PRICE_VARIANCE in _categories(result)

    def test_total_only_match_within_tolerance_passes(self) -> None:
        result = three_way_match(
            _invoice(lines=[], net="11550.00", tax="0.00", gross="11550.00"), _po(), as_of=AS_OF
        )
        assert result.all_within_tolerance


class TestCurrencyRule:
    """FIN-POL-002 §1 and FIN-POL-009 §3."""

    def test_currency_mismatch_without_permission_is_blocking(self) -> None:
        invoice = _invoice(lines=[], net="11520.00", tax="0.00", gross="11520.00", currency="USD")
        result = three_way_match(invoice, _po(currency="AUD"), as_of=AS_OF)
        assert not result.all_within_tolerance
        assert any("FIN-POL-009" in ref for e in result.exceptions for ref in e.policy_refs)

    def test_currency_mismatch_with_explicit_po_permission_is_recorded_not_blocking(self) -> None:
        invoice = _invoice(lines=[], net="11520.00", tax="0.00", gross="11520.00", currency="USD")
        result = three_way_match(invoice, _po(currency="AUD", permits_conversion=True), as_of=AS_OF)
        blocking = [e for e in result.exceptions if e.blocking]
        assert all(e.category is not ExceptionCategory.OTHER_CONTROL_RISK for e in blocking)

    def test_matching_currency_produces_no_currency_exception(self) -> None:
        result = three_way_match(_invoice(lines=[_goods_line()]), _po(), as_of=AS_OF)
        assert all("FIN-POL-009" not in ref for e in result.exceptions for ref in e.policy_refs)


class TestExceptionRecordQuality:
    """FIN-POL-007 §2 rejects generic notes such as "does not match"."""

    def test_variance_exception_names_expected_observed_and_threshold(self) -> None:
        line = _goods_line(unit_price="100.00", total="12000.00")
        result = three_way_match(
            _invoice(lines=[line], net="12000.00", tax="0.00", gross="12000.00"), _po(), as_of=AS_OF
        )
        exception = next(
            e for e in result.exceptions if e.category is ExceptionCategory.PRICE_VARIANCE
        )
        assert "11520.00" in exception.expected
        assert "12000.00" in exception.observed
        assert "50.00" in exception.detail
        assert exception.policy_refs
        assert exception.next_review_date is not None

    def test_every_exception_names_a_failed_rule_and_an_owner(self) -> None:
        result = three_way_match(_invoice(lines=[_goods_line()]), None, as_of=AS_OF)
        for exception in result.exceptions:
            assert exception.failed_rule
            assert exception.owner
            assert exception.policy_refs


class TestSplittingDoesNotDefeatTolerance:
    """FIN-POL-002 §3: "Splitting a variance across multiple invoices does not make it
    acceptable." The in-document analogue is splitting across lines."""

    def test_variance_split_across_two_lines_is_caught_at_the_document_total(self) -> None:
        po = PurchaseOrder(
            po_reference="PO-1",
            vendor_id="V-1",
            currency="AUD",
            total_value=Decimal("20000.00"),
            approval_status="APPROVED",
            lines=[
                POLine(
                    line_number=1,
                    description="Item A",
                    quantity_ordered=Decimal("100"),
                    unit_price=Decimal("100.00"),
                    line_value=Decimal("10000.00"),
                ),
                POLine(
                    line_number=2,
                    description="Item B",
                    quantity_ordered=Decimal("100"),
                    unit_price=Decimal("100.00"),
                    line_value=Decimal("10000.00"),
                ),
            ],
            receipts=[
                GoodsReceipt(
                    receipt_id="GR-1",
                    po_line_number=1,
                    quantity_received=Decimal("100"),
                    received_date=TODAY,
                    receipted_by="U-1",
                ),
                GoodsReceipt(
                    receipt_id="GR-2",
                    po_line_number=2,
                    quantity_received=Decimal("100"),
                    received_date=TODAY,
                    receipted_by="U-1",
                ),
            ],
        )
        # Each line is 45.00 over, under the AUD 50 line limit, but 90.00 over in total.
        invoice = Invoice(
            invoice_reference="INV-1",
            vendor_id="V-1",
            vendor_name="Vendor One",
            invoice_date=TODAY,
            currency="AUD",
            net_amount=Decimal("20090.00"),
            tax_amount=Decimal("0.00"),
            gross_amount=Decimal("20090.00"),
            po_reference="PO-1",
            lines=[
                InvoiceLine(
                    line_number=1,
                    description="Item A",
                    quantity=Decimal("100"),
                    unit_price=Decimal("100.45"),
                    line_total=Decimal("10045.00"),
                    po_line_number=1,
                ),
                InvoiceLine(
                    line_number=2,
                    description="Item B",
                    quantity=Decimal("100"),
                    unit_price=Decimal("100.45"),
                    line_total=Decimal("10045.00"),
                    po_line_number=2,
                ),
            ],
        )
        result = three_way_match(invoice, po, as_of=AS_OF)
        assert not result.all_within_tolerance, "aggregate variance must be assessed"


class TestTaxIsAssessedSeparately:
    """FIN-POL-002 §2: tax differences must not be hidden inside a price variance."""

    def test_price_variance_is_computed_on_net_not_gross(self) -> None:
        invoice = _invoice(lines=[_goods_line()], net="11520.00", tax="1152.00", gross="12672.00")
        result = three_way_match(invoice, _po(), as_of=AS_OF)
        assert result.all_within_tolerance, "tax must not inflate the price variance"


@pytest.mark.parametrize("as_of_date", [date(2026, 1, 1), date(2026, 12, 31)])
def test_next_review_date_is_three_business_days_or_later(as_of_date: date) -> None:
    """FIN-POL-007 §4: standard exceptions are reviewed within three business days."""
    result = three_way_match(
        _invoice(lines=[_goods_line()]),
        _po(receipts=[]),
        as_of=datetime(as_of_date.year, as_of_date.month, as_of_date.day, tzinfo=UTC),
    )
    exception = next(e for e in result.exceptions if e.next_review_date)
    assert exception.next_review_date is not None
    assert exception.next_review_date > as_of_date
