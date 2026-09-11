"""Separate tax assessment (FIN-POL-002 §2).

The gap these cover: FIN-POL-002 §2 requires tax to be "assessed separately" and forbids it
being "hidden inside a price variance". The matching engine honoured the second half by
comparing net against net, and nothing performed the first half, so ``TAX_QUERY`` — a category
FIN-POL-007 §1 defines — was unreachable.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from ap_agent.domain.enums import ExceptionCategory, LineType
from ap_agent.domain.evidence import Invoice, POLine, PurchaseOrder
from ap_agent.domain.rules.tax import EXPECTED_TAX_RATE_PERCENT, assess_tax

AS_OF = datetime(2026, 9, 11, 9, 0, tzinfo=UTC)


def _invoice(*, net: str, tax: str, currency: str = "AUD") -> Invoice:
    return Invoice(
        invoice_reference="INV-2026-0451",
        vendor_id="V-1001",
        vendor_name="Brightline Industrial Supplies Pty Ltd",
        invoice_date=date(2026, 9, 9),
        currency=currency,
        net_amount=Decimal(net),
        tax_amount=Decimal(tax),
        gross_amount=Decimal(net) + Decimal(tax),
        po_reference="PO-88121",
    )


def _order(*, total: str = "16320.00", currency: str = "AUD") -> PurchaseOrder:
    return PurchaseOrder(
        po_reference="PO-88121",
        vendor_id="V-1001",
        currency=currency,
        total_value=Decimal(total),
        approval_status="APPROVED",
        approved_by="U-3081",
        payment_terms_days=30,
        lines=[
            POLine(
                line_number=1,
                description="Industrial bearing assembly, 40mm",
                line_type=LineType.GOODS,
                quantity_ordered=Decimal("120"),
                unit_price=Decimal("136.00"),
                line_value=Decimal(total),
            )
        ],
    )


def _categories(result: object) -> set[ExceptionCategory]:
    return {exception.category for exception in result.exceptions}  # type: ignore[attr-defined]


class TestSeparatedTax:
    """An invoice that states its components is checked against the configured rate."""

    def test_tax_at_the_expected_rate_is_satisfied(self) -> None:
        result = assess_tax(
            _invoice(net="16320.00", tax="1632.00"),
            tax_separated=True,
            purchase_order=_order(),
            as_of=AS_OF,
        )
        assert not result.exceptions
        finding = next(f for f in result.findings if f.rule == "tax_assessed_separately")
        assert finding.satisfied

    def test_a_third_figure_raises_a_tax_query(self) -> None:
        """Neither zero nor the rate: a disagreement about tax treatment."""
        result = assess_tax(
            _invoice(net="16320.00", tax="900.00"),
            tax_separated=True,
            purchase_order=_order(),
            as_of=AS_OF,
        )
        assert ExceptionCategory.TAX_QUERY in _categories(result)

    def test_a_tax_query_goes_to_financial_control(self) -> None:
        """FIN-POL-007 §3 sends approval and treatment questions to Financial Control."""
        result = assess_tax(
            _invoice(net="16320.00", tax="900.00"),
            tax_separated=True,
            purchase_order=_order(),
            as_of=AS_OF,
        )
        assert result.exceptions[0].owner.value == "FINANCIAL_CONTROL"

    def test_zero_tax_is_not_queried(self) -> None:
        """GST-free and export supplies exist. Querying every zero would be noise."""
        result = assess_tax(
            _invoice(net="16320.00", tax="0.00"),
            tax_separated=True,
            purchase_order=_order(),
            as_of=AS_OF,
        )
        assert not result.exceptions
        finding = next(f for f in result.findings if f.rule == "tax_assessed_separately")
        assert finding.satisfied

    def test_a_few_cents_of_rounding_is_absorbed(self) -> None:
        """Tax is rounded per line before the lines are totalled."""
        result = assess_tax(
            _invoice(net="16320.00", tax="1632.04"),
            tax_separated=True,
            purchase_order=_order(),
            as_of=AS_OF,
        )
        assert not result.exceptions

    def test_the_rate_is_recorded_as_an_assumption(self) -> None:
        """The corpus states a jurisdiction and no rate, so the rate is configuration."""
        result = assess_tax(
            _invoice(net="16320.00", tax="1632.00"),
            tax_separated=True,
            purchase_order=_order(),
            as_of=AS_OF,
        )
        assert any(str(EXPECTED_TAX_RATE_PERCENT) in note for note in result.assumptions)

    def test_the_calculation_shows_the_expected_amount(self) -> None:
        result = assess_tax(
            _invoice(net="16320.00", tax="1632.00"),
            tax_separated=True,
            purchase_order=_order(),
            as_of=AS_OF,
        )
        calculation = next(
            c for c in result.calculations if c.name == "tax_expected_at_configured_rate"
        )
        assert calculation.result == Decimal("1632.00")


class TestUnseparatedTax:
    """A gross-only submission is where tax hides inside a price variance."""

    def test_a_variance_matching_the_tax_is_raised_as_a_tax_query(self) -> None:
        """The invoice total exceeds the order by exactly the tax on the order value."""
        result = assess_tax(
            _invoice(net="17952.00", tax="0.00"),
            tax_separated=False,
            purchase_order=_order(total="16320.00"),
            as_of=AS_OF,
        )
        assert ExceptionCategory.TAX_QUERY in _categories(result)

    def test_the_query_goes_to_the_requester_who_can_obtain_a_split_invoice(self) -> None:
        result = assess_tax(
            _invoice(net="17952.00", tax="0.00"),
            tax_separated=False,
            purchase_order=_order(total="16320.00"),
            as_of=AS_OF,
        )
        assert result.exceptions[0].owner.value == "REQUESTER"

    def test_a_variance_unlike_tax_is_left_to_the_price_comparison(self) -> None:
        """Not every unexplained difference is tax. This one is a third of the order value."""
        result = assess_tax(
            _invoice(net="22000.00", tax="0.00"),
            tax_separated=False,
            purchase_order=_order(total="16320.00"),
            as_of=AS_OF,
        )
        assert not result.exceptions

    def test_a_total_equal_to_the_order_raises_nothing(self) -> None:
        """A gross-only invoice that matches the order carries no tax to separate."""
        result = assess_tax(
            _invoice(net="16320.00", tax="0.00"),
            tax_separated=False,
            purchase_order=_order(total="16320.00"),
            as_of=AS_OF,
        )
        assert not result.exceptions

    def test_a_different_currency_is_not_compared(self) -> None:
        """FIN-POL-009 §2 forbids inventing a rate, so there is no variance to attribute."""
        result = assess_tax(
            _invoice(net="17952.00", tax="0.00", currency="USD"),
            tax_separated=False,
            purchase_order=_order(total="16320.00", currency="AUD"),
            as_of=AS_OF,
        )
        assert not result.exceptions
        assert not result.assessed

    def test_no_order_means_no_attribution(self) -> None:
        result = assess_tax(
            _invoice(net="17952.00", tax="0.00"),
            tax_separated=False,
            purchase_order=None,
            as_of=AS_OF,
        )
        assert not result.assessed
        finding = next(f for f in result.findings if f.rule == "tax_assessed_separately")
        assert not finding.satisfied

    def test_the_gross_only_substitution_is_recorded_as_an_assumption(self) -> None:
        """A reader must be able to see that the net used for matching was the gross."""
        result = assess_tax(
            _invoice(net="17952.00", tax="0.00"),
            tax_separated=False,
            purchase_order=_order(),
            as_of=AS_OF,
        )
        assert any("no tax component" in note for note in result.assumptions)
