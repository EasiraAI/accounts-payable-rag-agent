"""Invoice validity tests (FIN-POL-001 §2 and §3).

These cover the gap a controls review found: ``REJECT_INVALID`` is one of the five outcomes
FIN-POL-001 §3 permits, and nothing in the engine could ever produce it. The rule plumbing
for ``invalid_reasons`` existed and no control ever populated it, so a self-contradictory
invoice was carried into the tolerance engine and reported as a variance against the
purchase order, which blames the wrong party.

The line these tests hold is the one between *incomplete* and *wrong*. An absent document is
a hold under FIN-POL-001 §5. A document that does not add up is invalid.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from ap_agent.domain.enums import ExceptionCategory, LineType
from ap_agent.domain.evidence import Invoice, InvoiceLine
from ap_agent.domain.request import UntrustedText
from ap_agent.domain.rules.validity import ValidityResult, check_invoice_validity

AS_OF = datetime(2026, 9, 11, 9, 0, tzinfo=UTC)


def _line(*, number: int = 1, quantity: str = "10", unit_price: str = "100.00") -> InvoiceLine:
    return InvoiceLine(
        line_number=number,
        description=f"Test line {number}",
        line_type=LineType.GOODS,
        quantity=Decimal(quantity),
        unit_price=Decimal(unit_price),
        line_total=(Decimal(quantity) * Decimal(unit_price)).quantize(Decimal("0.01")),
        po_line_number=number,
    )


def _invoice(
    *,
    reference: str = "INV-2026-0451",
    net: str = "1000.00",
    tax: str = "100.00",
    invoice_date: date | None = None,
    po_reference: str | None = "PO-88121",
    lines: list[InvoiceLine] | None = None,
) -> Invoice:
    return Invoice(
        invoice_reference=reference,
        vendor_id="V-1001",
        vendor_name="Brightline Industrial Supplies Pty Ltd",
        invoice_date=invoice_date or date(2026, 9, 9),
        currency="AUD",
        net_amount=Decimal(net),
        tax_amount=Decimal(tax),
        gross_amount=Decimal(net) + Decimal(tax),
        po_reference=po_reference,
        lines=lines if lines is not None else [_line()],
    )


def _check(
    invoice: Invoice,
    *,
    invoice_date_supplied: bool = True,
    tax_separated: bool = True,
    texts: list[UntrustedText] | None = None,
) -> ValidityResult:
    """Check an invoice, defaulting to a submission that stated its own components.

    ``tax_separated`` defaults to True because that is the case the consistency test applies
    to: an invoice that gave a net and a tax can be held to its own arithmetic. The gross-only
    case has its own test below.
    """
    return check_invoice_validity(
        invoice,
        invoice_date_supplied=invoice_date_supplied,
        tax_separated=tax_separated,
        texts=texts or [],
        as_of=AS_OF,
    )


class TestInternalConsistency:
    """An invoice must agree with its own lines before it is compared with anything else."""

    def test_lines_that_sum_to_the_document_net_are_valid(self) -> None:
        result = _check(_invoice(), invoice_date_supplied=True)
        assert result.is_valid
        assert result.invalid_reasons == []

    def test_lines_that_do_not_sum_to_the_document_net_are_invalid(self) -> None:
        """Not a tolerance variance: no set of lines on this invoice supports its total."""
        result = _check(_invoice(net="1500.00"), invoice_date_supplied=True)
        assert not result.is_valid
        assert "1000.00" in result.invalid_reasons[0]
        assert "1500.00" in result.invalid_reasons[0]

    def test_the_exception_names_the_requester_not_the_vendor_governance_team(self) -> None:
        """FIN-POL-007 §3 sends PO and document issues to the requester."""
        result = _check(_invoice(net="1500.00"), invoice_date_supplied=True)
        exception = result.exceptions[0]
        assert exception.category is ExceptionCategory.OTHER_CONTROL_RISK
        assert exception.owner.value == "REQUESTER"

    def test_per_line_rounding_is_absorbed(self) -> None:
        """A cent a line is supplier rounding, not an arithmetic fault."""
        lines = [_line(number=1), _line(number=2)]
        net = sum(line.line_total for line in lines) + Decimal("0.02")
        result = _check(_invoice(net=str(net), lines=lines), invoice_date_supplied=True)
        assert result.is_valid

    def test_slack_does_not_grow_without_lines_to_justify_it(self) -> None:
        """Two cents is within slack for two lines and outside it for one."""
        result = _check(_invoice(net="1000.02"), invoice_date_supplied=True)
        assert not result.is_valid

    def test_an_invoice_without_lines_is_not_assessed_for_consistency(self) -> None:
        """Nothing to compare. The document total is matched against the order instead."""
        result = _check(_invoice(lines=[]), invoice_date_supplied=True)
        assert result.is_valid
        assert not any(
            calculation.name == "invoice_internal_consistency"
            for calculation in result.calculations
        )

    def test_the_calculation_is_recorded_for_the_audit_trail(self) -> None:
        """FIN-POL-001 §6 requires the calculations performed to be on the case record."""
        result = _check(_invoice(), invoice_date_supplied=True)
        calculation = next(
            c for c in result.calculations if c.name == "invoice_internal_consistency"
        )
        assert calculation.formula == "invoice_net - sum(line_total)"
        assert calculation.currency == "AUD"


class TestFutureDating:
    """FIN-POL-006 §1 runs terms from receipt; FIN-POL-011 §1 assigns a period by date."""

    def test_an_invoice_dated_after_today_is_invalid(self) -> None:
        result = _check(_invoice(invoice_date=date(2026, 9, 15)), invoice_date_supplied=True)
        assert not result.is_valid
        assert "2026-09-15" in result.invalid_reasons[0]

    def test_an_invoice_dated_today_is_valid(self) -> None:
        """The boundary is inclusive: an invoice received the day it was raised is normal."""
        result = _check(_invoice(invoice_date=AS_OF.date()), invoice_date_supplied=True)
        assert result.is_valid

    def test_a_defaulted_date_is_never_treated_as_future_dated(self) -> None:
        """The request substitutes today when the document carried no date.

        Testing the substituted value would reject a case for a date this system invented.
        """
        result = _check(_invoice(invoice_date=date(2026, 12, 31)), invoice_date_supplied=False)
        assert result.is_valid


class TestMinimumEvidence:
    """FIN-POL-001 §2 names the fields a case must contain."""

    def test_a_complete_case_records_a_satisfied_finding(self) -> None:
        """Visible rather than inferred: a reviewer should see the check, not its silence."""
        result = _check(_invoice(), invoice_date_supplied=True)
        finding = next(f for f in result.findings if f.rule == "minimum_evidence_present")
        assert finding.satisfied
        assert "invoice date" in finding.detail

    def test_an_absent_purchase_order_is_reported_but_not_invalid(self) -> None:
        """A gap a person can close is a hold under FIN-POL-001 §5, not a rejection."""
        result = _check(_invoice(po_reference=None), invoice_date_supplied=True)
        finding = next(f for f in result.findings if f.rule == "minimum_evidence_present")
        assert not finding.satisfied
        assert result.is_valid

    def test_an_absent_purchase_order_says_the_alternative_cannot_be_assessed(self) -> None:
        """FIN-POL-001 §2 accepts a purchase order *or* an approved non-PO justification.

        The second alternative is not representable: the request carries no field for it, and
        FIN-POL-012 §1 limits non-PO processing to specific categories. An earlier version took
        a boolean parameter that no caller ever set, so the finding asserted that no
        justification existed when it had no way of knowing. Saying so is the honest position.
        """
        result = _check(_invoice(po_reference=None))
        finding = next(f for f in result.findings if f.rule == "minimum_evidence_present")
        assert "no field for one" in finding.detail

    def test_an_absent_invoice_date_is_reported_as_absent(self) -> None:
        result = _check(_invoice(), invoice_date_supplied=False)
        finding = next(f for f in result.findings if f.rule == "minimum_evidence_present")
        assert not finding.satisfied
        assert "invoice date" in finding.detail.split("absent:")[1]


class TestGrossOnlySubmission:
    """A submission that did not separate its tax must not be judged on a substituted net.

    The defect: ``to_invoice`` uses the gross as the net when no components are supplied, so
    tax-exclusive lines fall short of it by exactly the tax. The consistency test then rejected
    a valid invoice as invalid, pre-empting the tax assessment that exists to explain the same
    difference — and ``REJECT_INVALID`` outranks a hold and is consequential.
    """

    def test_lines_short_of_a_substituted_net_are_not_invalid(self) -> None:
        # Lines sum to 1,000; the gross of 1,100 stands in for the net.
        result = _check(_invoice(net="1100.00", tax="0.00"), tax_separated=False)
        assert result.is_valid

    def test_the_consistency_calculation_is_not_recorded_either(self) -> None:
        """Recording a comparison against a figure this system invented would mislead."""
        result = _check(_invoice(net="1100.00", tax="0.00"), tax_separated=False)
        assert not any(
            calculation.name == "invoice_internal_consistency"
            for calculation in result.calculations
        )

    def test_a_separated_submission_is_still_checked(self) -> None:
        """The guard must not disable the test for the case it was written for."""
        result = _check(_invoice(net="1500.00"))
        assert not result.is_valid


class TestCreditNotes:
    """FIN-POL-008 §1: a credit note must not be treated as a negative invoice.

    §1 requires tax and accounting treatment to be validated first, and §2 governs the order
    credits are applied in. Neither is implemented, and the request schema requires a positive
    amount, so a credit note can only arrive mistyped as an invoice — where matching, duplicate
    detection and the tax assessment would all judge it against rules written for an obligation
    to pay. A controls review found no code path that noticed.
    """

    def test_a_credit_note_reference_is_recognised(self) -> None:
        result = _check(_invoice(reference="CREDIT NOTE 2026-0451"))
        assert any(
            exception.failed_rule == "validity.document_is_an_invoice_not_a_credit_note"
            for exception in result.exceptions
        )

    def test_credit_note_wording_in_the_case_text_is_recognised(self) -> None:
        result = _check(
            _invoice(),
            texts=[
                UntrustedText(
                    content="This adjustment note reverses invoice INV-2026-0451.",
                    origin="case notes",
                )
            ],
        )
        assert any(
            exception.failed_rule == "validity.document_is_an_invoice_not_a_credit_note"
            for exception in result.exceptions
        )

    def test_a_credit_note_is_held_rather_than_rejected(self) -> None:
        """Held, because the treatment has to be validated by a person, not guessed."""
        result = _check(_invoice(reference="CN-2026-0451 credit note"))
        assert result.is_valid  # not REJECT_INVALID
        assert all(exception.blocking for exception in result.exceptions)

    def test_it_goes_to_financial_control(self) -> None:
        """FIN-POL-008 §1 makes the treatment Financial Control's call."""
        result = _check(_invoice(reference="Credit Memo 991"))
        exception = next(
            e
            for e in result.exceptions
            if e.failed_rule == "validity.document_is_an_invoice_not_a_credit_note"
        )
        assert exception.owner.value == "FINANCIAL_CONTROL"

    def test_an_ordinary_invoice_is_not_mistaken_for_one(self) -> None:
        result = _check(_invoice())
        assert not result.exceptions
