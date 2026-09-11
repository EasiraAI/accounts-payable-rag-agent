"""Property-based tests for the invariants the rule engine must hold everywhere.

Example-based tests check the thresholds at the points someone thought to check. These
check the properties across the input space, which is where an arithmetic or ordering
mistake actually hides. Each property is stated as a claim about the system, not about an
implementation detail, so the tests survive refactoring.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from ap_agent.domain.enums import ApproverRole, ExceptionCategory, LineType, Outcome
from ap_agent.domain.evidence import GoodsReceipt, Invoice, InvoiceLine, POLine, PurchaseOrder
from ap_agent.domain.money import Money, minimum, percent_of, quantize
from ap_agent.domain.rules.authority import AUTHORITY_LIMITS, required_authority
from ap_agent.domain.rules.matching import three_way_match

AS_OF = datetime(2026, 9, 11, tzinfo=UTC)
TODAY: date = AS_OF.date()

#: Amounts are generated as integer cents and converted, so the strategy never produces a
#: float and never depends on float rounding to describe the value it intends.
cents = st.integers(min_value=1, max_value=50_000_00)


def money(value_cents: int, currency: str = "AUD") -> Decimal:
    return (Decimal(value_cents) / Decimal(100)).quantize(Decimal("0.01"))


# ---- monetary arithmetic --------------------------------------------------------------


class TestMoneyProperties:
    @given(cents, cents)
    def test_addition_then_subtraction_is_exact(self, a: int, b: int) -> None:
        """The property float arithmetic fails: (a + b) - b == a for every value."""
        first = Money.of(money(a), "AUD")
        second = Money.of(money(b), "AUD")
        assert (first + second) - second == first

    @given(cents)
    def test_quantize_is_idempotent(self, a: int) -> None:
        once = quantize(money(a))
        assert quantize(once) == once

    @given(cents, st.integers(min_value=0, max_value=100))
    def test_percent_of_is_monotone_in_the_percentage(self, a: int, percent: int) -> None:
        base = Money.of(money(a), "AUD")
        lower = percent_of(base, Decimal(percent))
        higher = percent_of(base, Decimal(percent + 1))
        assert lower.amount <= higher.amount

    @given(cents, cents)
    def test_minimum_returns_one_of_its_arguments(self, a: int, b: int) -> None:
        first = Money.of(money(a), "AUD")
        second = Money.of(money(b), "AUD")
        chosen = minimum(first, second)
        assert chosen in {first, second}
        assert chosen.amount <= first.amount
        assert chosen.amount <= second.amount


# ---- tolerance behaviour --------------------------------------------------------------


def _single_line_case(
    *, po_unit_price: Decimal, invoiced_unit_price: Decimal, quantity: Decimal
) -> tuple[Invoice, PurchaseOrder]:
    po_line_value = quantize(quantity * po_unit_price)
    invoiced_total = quantize(quantity * invoiced_unit_price)
    po = PurchaseOrder(
        po_reference="PO-P",
        vendor_id="V-P",
        currency="AUD",
        total_value=po_line_value,
        approval_status="APPROVED",
        lines=[
            POLine(
                line_number=1,
                description="Item",
                line_type=LineType.GOODS,
                quantity_ordered=quantity,
                unit_price=po_unit_price,
                line_value=po_line_value,
            )
        ],
        receipts=[
            GoodsReceipt(
                receipt_id="GR-P",
                po_line_number=1,
                quantity_received=quantity,
                received_date=TODAY,
                receipted_by="U-P",
            )
        ],
    )
    invoice = Invoice(
        invoice_reference="INV-P",
        vendor_id="V-P",
        vendor_name="Vendor P",
        invoice_date=TODAY,
        currency="AUD",
        net_amount=invoiced_total,
        tax_amount=Decimal("0.00"),
        gross_amount=invoiced_total,
        po_reference="PO-P",
        lines=[
            InvoiceLine(
                line_number=1,
                description="Item",
                line_type=LineType.GOODS,
                quantity=quantity,
                unit_price=invoiced_unit_price,
                line_total=invoiced_total,
                # Required: a line without a purchase-order line reference is correctly
                # reported as unmapped, which would mask the tolerance property under test.
                po_line_number=1,
            )
        ],
    )
    return invoice, po


class TestToleranceProperties:
    @given(
        quantity=st.integers(min_value=1, max_value=500),
        po_price_cents=st.integers(min_value=100, max_value=100_000),
        extra_cents=st.integers(min_value=0, max_value=500_00),
    )
    @settings(max_examples=200, deadline=None)
    def test_a_zero_variance_line_is_always_within_tolerance(
        self, quantity: int, po_price_cents: int, extra_cents: int
    ) -> None:
        """Invoicing exactly the ordered price is never a variance, at any scale."""
        price = money(po_price_cents)
        invoice, po = _single_line_case(
            po_unit_price=price, invoiced_unit_price=price, quantity=Decimal(quantity)
        )
        result = three_way_match(invoice, po, as_of=AS_OF)
        assert result.all_within_tolerance
        assert not any(
            exception.category is ExceptionCategory.PRICE_VARIANCE
            for exception in result.exceptions
        )

    @given(
        quantity=st.integers(min_value=1, max_value=200),
        po_price_cents=st.integers(min_value=1_000, max_value=50_000),
        overcharge_cents=st.integers(min_value=1, max_value=200_00),
    )
    @settings(max_examples=200, deadline=None)
    def test_overcharging_beyond_the_limit_always_raises_a_variance(
        self, quantity: int, po_price_cents: int, overcharge_cents: int
    ) -> None:
        """If the computed variance exceeds the computed limit, an exception must exist.

        The test recomputes the expected limit independently of the engine, so agreement is
        evidence rather than tautology.
        """
        price = money(po_price_cents)
        quantity_decimal = Decimal(quantity)
        line_value = quantize(quantity_decimal * price)
        limit = minimum(
            Money.of("50.00", "AUD"),
            percent_of(Money.of(line_value, "AUD"), Decimal("1")),
        ).amount

        total_overcharge = money(overcharge_cents)
        per_unit_extra = (total_overcharge / quantity_decimal).quantize(Decimal("0.0001"))
        invoiced_price = price + per_unit_extra
        invoice, po = _single_line_case(
            po_unit_price=price, invoiced_unit_price=invoiced_price, quantity=quantity_decimal
        )
        actual_variance = abs(invoice.net_amount - line_value)
        assume(actual_variance > limit)

        result = three_way_match(invoice, po, as_of=AS_OF)
        assert not result.all_within_tolerance
        assert any(
            exception.category is ExceptionCategory.PRICE_VARIANCE
            for exception in result.exceptions
        )

    @given(
        line_count=st.integers(min_value=2, max_value=8),
        per_line_value_cents=st.integers(min_value=100_00, max_value=500_00),
        total_overcharge_cents=st.integers(min_value=60_00, max_value=400_00),
    )
    @settings(max_examples=150, deadline=None)
    def test_splitting_a_variance_across_lines_never_makes_it_acceptable(
        self, line_count: int, per_line_value_cents: int, total_overcharge_cents: int
    ) -> None:
        """FIN-POL-002 §3, expressed as a property.

        A total variance above the document limit must be caught however finely it is
        divided across lines, even when every individual slice sits inside its own line
        limit. This is the invariant a summed document limit would violate.
        """
        per_line_value = money(per_line_value_cents)
        total_overcharge = money(total_overcharge_cents)
        per_line_overcharge = (total_overcharge / Decimal(line_count)).quantize(Decimal("0.01"))
        # Recompute the actual total after per-line rounding.
        actual_total_overcharge = per_line_overcharge * Decimal(line_count)

        document_limit = minimum(
            Money.of("50.00", "AUD"),
            percent_of(Money.of(per_line_value * Decimal(line_count), "AUD"), Decimal("1")),
        ).amount
        assume(actual_total_overcharge > document_limit)

        po_lines = [
            POLine(
                line_number=index + 1,
                description=f"Item {index + 1}",
                line_type=LineType.GOODS,
                quantity_ordered=Decimal("1"),
                unit_price=per_line_value,
                line_value=per_line_value,
            )
            for index in range(line_count)
        ]
        receipts = [
            GoodsReceipt(
                receipt_id=f"GR-{index + 1}",
                po_line_number=index + 1,
                quantity_received=Decimal("1"),
                received_date=TODAY,
                receipted_by="U-P",
            )
            for index in range(line_count)
        ]
        po = PurchaseOrder(
            po_reference="PO-S",
            vendor_id="V-S",
            currency="AUD",
            total_value=per_line_value * Decimal(line_count),
            approval_status="APPROVED",
            lines=po_lines,
            receipts=receipts,
        )
        invoiced_line_total = per_line_value + per_line_overcharge
        invoice = Invoice(
            invoice_reference="INV-S",
            vendor_id="V-S",
            vendor_name="Vendor S",
            invoice_date=TODAY,
            currency="AUD",
            net_amount=invoiced_line_total * Decimal(line_count),
            tax_amount=Decimal("0.00"),
            gross_amount=invoiced_line_total * Decimal(line_count),
            po_reference="PO-S",
            lines=[
                InvoiceLine(
                    line_number=index + 1,
                    description=f"Item {index + 1}",
                    line_type=LineType.GOODS,
                    quantity=Decimal("1"),
                    unit_price=invoiced_line_total,
                    line_total=invoiced_line_total,
                    po_line_number=index + 1,
                )
                for index in range(line_count)
            ],
        )
        result = three_way_match(invoice, po, as_of=AS_OF)
        assert not result.all_within_tolerance, (
            f"{line_count} lines, {per_line_overcharge} each, total "
            f"{actual_total_overcharge} against document limit {document_limit}"
        )

    @given(
        quantity_ordered=st.integers(min_value=1, max_value=100),
        quantity_invoiced=st.integers(min_value=1, max_value=200),
    )
    @settings(max_examples=150, deadline=None)
    def test_quantity_exception_appears_exactly_when_invoiced_exceeds_received(
        self, quantity_ordered: int, quantity_invoiced: int
    ) -> None:
        """The quantity rule is an inequality, so it must hold in both directions."""
        price = Decimal("100.00")
        po = PurchaseOrder(
            po_reference="PO-Q",
            vendor_id="V-Q",
            currency="AUD",
            total_value=quantize(Decimal(quantity_ordered) * price),
            approval_status="APPROVED",
            lines=[
                POLine(
                    line_number=1,
                    description="Item",
                    line_type=LineType.GOODS,
                    quantity_ordered=Decimal(quantity_ordered),
                    unit_price=price,
                    line_value=quantize(Decimal(quantity_ordered) * price),
                )
            ],
            receipts=[
                GoodsReceipt(
                    receipt_id="GR-Q",
                    po_line_number=1,
                    quantity_received=Decimal(quantity_ordered),
                    received_date=TODAY,
                    receipted_by="U-Q",
                )
            ],
        )
        invoiced_total = quantize(Decimal(quantity_invoiced) * price)
        invoice = Invoice(
            invoice_reference="INV-Q",
            vendor_id="V-Q",
            vendor_name="Vendor Q",
            invoice_date=TODAY,
            currency="AUD",
            net_amount=invoiced_total,
            tax_amount=Decimal("0.00"),
            gross_amount=invoiced_total,
            po_reference="PO-Q",
            lines=[
                InvoiceLine(
                    line_number=1,
                    description="Item",
                    line_type=LineType.GOODS,
                    quantity=Decimal(quantity_invoiced),
                    unit_price=price,
                    line_total=invoiced_total,
                    po_line_number=1,
                )
            ],
        )
        result = three_way_match(invoice, po, as_of=AS_OF)
        has_quantity_exception = any(
            exception.category is ExceptionCategory.QUANTITY_VARIANCE
            for exception in result.exceptions
        )
        assert has_quantity_exception == (quantity_invoiced > quantity_ordered)


# ---- authority behaviour --------------------------------------------------------------


class TestAuthorityProperties:
    @given(st.integers(min_value=1, max_value=5_000_000_00))
    @settings(max_examples=300, deadline=None)
    def test_the_required_role_always_covers_the_amount(self, amount_cents: int) -> None:
        """The chosen role's limit must never be below the amount it was chosen for."""
        amount = money(amount_cents)
        requirement = required_authority(Money.of(amount, "AUD"), higher_risk_reasons=[])
        limit = AUTHORITY_LIMITS.get(requirement.required_role_minimum)
        if limit is None:
            assert requirement.required_role_minimum is ApproverRole.CEO
            assert amount > AUTHORITY_LIMITS[ApproverRole.CFO]
        else:
            assert amount <= limit

    @given(st.integers(min_value=1, max_value=5_000_000_00))
    @settings(max_examples=300, deadline=None)
    def test_the_required_role_is_the_lowest_that_covers_the_amount(
        self, amount_cents: int
    ) -> None:
        """No role below the chosen one may have a sufficient limit.

        This is what makes the matrix a least-authority ladder rather than a suggestion.
        """
        amount = money(amount_cents)
        requirement = required_authority(Money.of(amount, "AUD"), higher_risk_reasons=[])
        for role, limit in AUTHORITY_LIMITS.items():
            if limit < (AUTHORITY_LIMITS.get(requirement.required_role_minimum) or limit + 1):
                assert amount > limit, (
                    f"{role.value} limit {limit} would have covered {amount}, "
                    f"but {requirement.required_role_minimum.value} was required"
                )

    @given(
        st.integers(min_value=1, max_value=1_000_000_00),
        st.integers(min_value=1, max_value=1_000_000_00),
    )
    @settings(max_examples=200, deadline=None)
    def test_authority_requirement_is_monotone_in_amount(self, a_cents: int, b_cents: int) -> None:
        """A larger amount never needs a lower role. Splitting cannot reduce authority."""
        lower_cents, higher_cents = sorted((a_cents, b_cents))
        ladder = [*AUTHORITY_LIMITS, ApproverRole.CEO]
        lower = required_authority(Money.of(money(lower_cents), "AUD"), higher_risk_reasons=[])
        higher = required_authority(Money.of(money(higher_cents), "AUD"), higher_risk_reasons=[])
        assert ladder.index(higher.required_role_minimum) >= ladder.index(
            lower.required_role_minimum
        )

    @given(st.integers(min_value=1, max_value=1_000_000_00), st.integers(min_value=2, max_value=10))
    @settings(max_examples=200, deadline=None)
    def test_splitting_an_amount_never_lowers_the_aggregate_requirement(
        self, total_cents: int, parts: int
    ) -> None:
        """FIN-POL-003 §1: "Amounts must not be split to avoid a threshold."

        Splitting is prevented by assessing the total commitment. The property checked here
        is the arithmetic fact that makes the control effective: the role required for the
        whole is never lower than the role required for any part.
        """
        total = money(total_cents)
        part = (total / Decimal(parts)).quantize(Decimal("0.01"))
        ladder = [*AUTHORITY_LIMITS, ApproverRole.CEO]
        whole = required_authority(Money.of(total, "AUD"), higher_risk_reasons=[])
        piece = required_authority(Money.of(part, "AUD"), higher_risk_reasons=[])
        assert ladder.index(whole.required_role_minimum) >= ladder.index(
            piece.required_role_minimum
        )


# ---- outcome behaviour ----------------------------------------------------------------


class TestOutcomeProperties:
    @given(st.integers(min_value=0, max_value=6))
    def test_only_one_outcome_ever_proposes_payment(self, _unused: int) -> None:
        """A stability property over the enum: exactly one outcome can move money."""
        proposing = [outcome for outcome in Outcome if outcome.proposes_payment]
        assert proposing == [Outcome.APPROVE_FOR_POSTING]

    @given(st.sampled_from(list(Outcome)))
    def test_every_consequential_outcome_is_a_posting_or_a_rejection(
        self, outcome: Outcome
    ) -> None:
        """Consequential means "writes to the ledger of record". Holds and escalations do not."""
        if outcome.is_consequential:
            assert outcome in {
                Outcome.APPROVE_FOR_POSTING,
                Outcome.REJECT_DUPLICATE,
                Outcome.REJECT_INVALID,
            }
        else:
            assert outcome in {Outcome.HOLD_FOR_INFORMATION, Outcome.ESCALATE_CONTROL_REVIEW}
