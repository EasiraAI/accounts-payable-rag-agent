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


def _order(
    *, total: str = "16320.00", currency: str = "AUD", permits_freight: bool = False
) -> PurchaseOrder:
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
                permits_freight=permits_freight,
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

    def test_an_overstated_tax_raises_a_tax_query(self) -> None:
        """More tax than the configured rate can produce has no innocent reading."""
        result = assess_tax(
            _invoice(net="16320.00", tax="2000.00"),
            tax_separated=True,
            purchase_order=_order(),
            as_of=AS_OF,
        )
        assert ExceptionCategory.TAX_QUERY in _categories(result)

    def test_an_understated_tax_is_recorded_and_not_queried(self) -> None:
        """A partly GST-free supply produces exactly this, and no clause forbids one.

        The first version of this module queried any difference and blocked on it, so a
        mixed taxable and GST-free invoice was held on a rate the corpus never states.
        """
        result = assess_tax(
            _invoice(net="16320.00", tax="900.00"),
            tax_separated=True,
            purchase_order=_order(),
            as_of=AS_OF,
        )
        assert not result.exceptions
        finding = next(f for f in result.findings if f.rule == "tax_assessed_separately")
        assert finding.satisfied
        assert "GST-free" in finding.detail

    def test_a_tax_query_never_blocks(self) -> None:
        """A blocking exception forces HOLD_FOR_INFORMATION.

        The rate is configuration, not policy, so a figure derived from it must not stop a
        payment whose price, receipt, vendor and authority all check out.
        """
        result = assess_tax(
            _invoice(net="16320.00", tax="2000.00"),
            tax_separated=True,
            purchase_order=_order(),
            as_of=AS_OF,
        )
        assert not any(exception.blocking for exception in result.exceptions)

    def test_the_query_says_the_rate_is_not_from_policy(self) -> None:
        """FIN-POL-002 §2 requires a separate assessment and sets no rate."""
        result = assess_tax(
            _invoice(net="16320.00", tax="2000.00"),
            tax_separated=True,
            purchase_order=_order(),
            as_of=AS_OF,
        )
        assert "states a tax rate" in result.exceptions[0].detail

    def test_a_tax_query_goes_to_financial_control(self) -> None:
        """FIN-POL-007 §3 sends approval and treatment questions to Financial Control."""
        result = assess_tax(
            _invoice(net="16320.00", tax="2000.00"),
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

    def test_a_variance_unlike_tax_is_still_reported_as_unattributed(self) -> None:
        """The fact is the missing separation, not a numeric coincidence.

        An earlier version raised the query only when the difference matched the configured
        rate, which made the control depend on an arithmetic accident: a genuine ten per cent
        overcharge was relabelled as tax, and a difference of any other size on an invoice
        that stated no components was passed to the price comparison as though the components
        were known.
        """
        result = assess_tax(
            _invoice(net="22000.00", tax="0.00"),
            tax_separated=False,
            purchase_order=_order(total="16320.00"),
            as_of=AS_OF,
        )
        assert ExceptionCategory.TAX_QUERY in _categories(result)
        assert "not obviously tax" in result.exceptions[0].detail

    def test_an_unattributed_difference_does_not_block(self) -> None:
        """The price comparison holds the case on its own terms if it is out of tolerance."""
        result = assess_tax(
            _invoice(net="22000.00", tax="0.00"),
            tax_separated=False,
            purchase_order=_order(total="16320.00"),
            as_of=AS_OF,
        )
        assert not any(exception.blocking for exception in result.exceptions)

    def test_an_order_permitting_freight_is_left_to_the_price_comparison(self) -> None:
        """FIN-POL-002 §2 allows freight to vary by up to AUD 75 where the order permits it.

        On such an order an unexplained difference has a likelier explanation than tax, and
        calling it a tax question would send it to the wrong owner.
        """
        result = assess_tax(
            _invoice(net="16390.00", tax="0.00"),
            tax_separated=False,
            purchase_order=_order(total="16320.00", permits_freight=True),
            as_of=AS_OF,
        )
        assert not result.exceptions
        finding = next(f for f in result.findings if f.rule == "tax_assessed_separately")
        assert "permits freight" in finding.detail

    def test_rounding_and_fx_are_recorded_as_assessed(self) -> None:
        """§2 names three separate assessments; a reviewer should see all three applied."""
        result = assess_tax(
            _invoice(net="17952.00", tax="0.00"),
            tax_separated=False,
            purchase_order=_order(total="16320.00"),
            as_of=AS_OF,
        )
        finding = next(
            f for f in result.findings if f.rule == "rounding_and_fx_assessed_separately"
        )
        assert finding.satisfied

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

    def test_australian_gst_is_not_applied_outside_the_policy_currency(self) -> None:
        """A USD invoice against a USD order carries no GST, whatever the totals say.

        The guard is the currency, not the mismatch: an earlier version compared any invoice
        and order that agreed with each other, and would have attributed a difference on a
        wholly foreign transaction to Australian tax.
        """
        result = assess_tax(
            _invoice(net="17952.00", tax="0.00", currency="USD"),
            tax_separated=False,
            purchase_order=_order(total="16320.00", currency="USD"),
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
