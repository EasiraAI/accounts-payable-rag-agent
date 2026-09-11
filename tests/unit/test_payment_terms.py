"""Payment terms and payment-run scheduling (FIN-POL-006 §1 to §3).

The gap these cover: ``payment_terms_days`` sat on the request schema and no control read it.
FIN-POL-006 §1 makes the printed figure a supplier claim that the agreed terms override, so
carrying it unread is the same defect the delegation scope had, where a stored field was never
compared and therefore conferred something it should not.

The dates below are chosen against a known week. 2026-09-11 is a Friday, so a 30-day term
from 2026-09-09 falls on 2026-10-09, also a Friday, and shifting the invoice date by a few
days walks the due date onto a weekend.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from ap_agent.domain.enums import LineType
from ap_agent.domain.evidence import Invoice, POLine, PurchaseOrder
from ap_agent.domain.rules.payment_terms import (
    DEFAULT_TERMS_DAYS,
    STANDARD_RUN_WEEKDAYS,
    PaymentTermsResult,
    assess_payment_terms,
)

AS_OF = datetime(2026, 9, 11, 9, 0, tzinfo=UTC)


def _invoice(*, invoice_date: date | None = None) -> Invoice:
    return Invoice(
        invoice_reference="INV-2026-0451",
        vendor_id="V-1001",
        vendor_name="Brightline Industrial Supplies Pty Ltd",
        invoice_date=invoice_date or date(2026, 9, 9),
        currency="AUD",
        net_amount=Decimal("16320.00"),
        tax_amount=Decimal("1632.00"),
        gross_amount=Decimal("17952.00"),
        po_reference="PO-88121",
    )


def _order(*, terms: int | None = 30) -> PurchaseOrder:
    return PurchaseOrder(
        po_reference="PO-88121",
        vendor_id="V-1001",
        currency="AUD",
        total_value=Decimal("16320.00"),
        approval_status="APPROVED",
        approved_by="U-3081",
        payment_terms_days=terms,
        lines=[
            POLine(
                line_number=1,
                description="Industrial bearing assembly, 40mm",
                line_type=LineType.GOODS,
                quantity_ordered=Decimal("120"),
                unit_price=Decimal("136.00"),
                line_value=Decimal("16320.00"),
            )
        ],
    )


def _assess(
    *,
    invoice: Invoice | None = None,
    purchase_order: PurchaseOrder | None = _order(),
    printed_terms_days: int | None = 30,
    invoice_date_supplied: bool = True,
) -> PaymentTermsResult:
    """Assess terms for a default case, with any input overridden.

    Spelled out rather than passed through ``**kwargs`` so the helper keeps the rule's types:
    a test that misnames an argument fails at the type check rather than silently assessing
    the default case and asserting on the wrong thing.
    """
    return assess_payment_terms(
        invoice if invoice is not None else _invoice(),
        purchase_order=purchase_order,
        printed_terms_days=printed_terms_days,
        invoice_date_supplied=invoice_date_supplied,
        as_of=AS_OF,
    )


class TestAgreedTerms:
    """FIN-POL-006 §1: the order's terms override the invoice's printed terms."""

    def test_the_order_supplies_the_agreed_terms(self) -> None:
        result = _assess(purchase_order=_order(terms=45), printed_terms_days=45)
        assert result.agreed_terms_days == 45
        assert "PO-88121" in result.terms_source

    def test_the_default_applies_when_the_order_records_none(self) -> None:
        result = _assess(purchase_order=_order(terms=None), printed_terms_days=None)
        assert result.agreed_terms_days == DEFAULT_TERMS_DAYS

    def test_the_default_applies_when_there_is_no_order(self) -> None:
        result = _assess(purchase_order=None, printed_terms_days=None)
        assert result.agreed_terms_days == DEFAULT_TERMS_DAYS

    def test_printed_terms_that_disagree_do_not_override(self) -> None:
        """A supplier shortening its own terms on the document must not shorten them here."""
        result = _assess(purchase_order=_order(terms=30), printed_terms_days=7)
        assert result.agreed_terms_days == 30
        assert result.printed_terms_conflict

    def test_the_disagreement_is_recorded_without_blocking_the_case(self) -> None:
        """§1 resolves the conflict, so the case proceeds. The trace is what matters."""
        result = _assess(printed_terms_days=7)
        exception = next(
            e
            for e in result.exceptions
            if e.failed_rule == "payment_terms.printed_terms_match_agreed_terms"
        )
        assert not exception.blocking
        assert "7 days" in exception.observed

    def test_matching_terms_raise_nothing(self) -> None:
        result = _assess(printed_terms_days=30)
        assert not result.printed_terms_conflict
        assert not result.exceptions


class TestDueDate:
    """FIN-POL-006 §1 and §2: calendar days, then off a non-business day."""

    def test_the_due_date_is_calendar_days_from_the_invoice_date(self) -> None:
        result = _assess(invoice=_invoice(invoice_date=date(2026, 9, 9)))
        assert result.due_date == date(2026, 10, 9)

    def test_a_weekend_due_date_moves_to_the_preceding_business_day(self) -> None:
        """Moving forward would make the payment late, which §2 avoids by moving back."""
        result = _assess(invoice=_invoice(invoice_date=date(2026, 9, 10)))
        assert result.due_date == date(2026, 10, 10)
        assert result.due_date.weekday() == 5
        assert result.payable_on == date(2026, 10, 9)

    def test_a_weekend_due_date_is_recorded_as_such(self) -> None:
        """Recorded for the schedule only.

        A version of this fed the flag to the weekend manual-payment indicator, which was
        wrong twice over: the payment does not settle on that day, because §2 has already
        moved it, and a due date is arithmetic rather than something a supplier asked for.
        """
        result = _assess(invoice=_invoice(invoice_date=date(2026, 9, 10)))
        assert result.due_date_on_non_business_day
        assert result.payable_on.weekday() < 5

    def test_a_weekday_due_date_is_left_alone(self) -> None:
        result = _assess(invoice=_invoice(invoice_date=date(2026, 9, 9)))
        assert result.payable_on == result.due_date
        assert not result.due_date_on_non_business_day

    def test_a_substituted_invoice_date_is_recorded_as_an_assumption(self) -> None:
        result = _assess(invoice_date_supplied=False)
        assert any("no invoice date" in note.lower() for note in result.assumptions)


class TestProposedRun:
    """FIN-POL-006 §2: Tuesday and Thursday, before the due date."""

    def test_the_proposal_is_a_standard_run_day(self) -> None:
        result = _assess()
        assert result.proposed_run_date is not None
        assert result.proposed_run_date.weekday() in STANDARD_RUN_WEEKDAYS

    def test_the_proposal_is_not_after_the_due_date(self) -> None:
        """Later than the due date would be a late payment, whatever the run calendar says."""
        result = _assess()
        assert result.proposed_run_date <= result.payable_on

    def test_the_proposal_is_the_last_run_that_still_meets_the_terms(self) -> None:
        """Paying earlier without a commercial benefit would be an early payment under §3."""
        result = _assess(invoice=_invoice(invoice_date=date(2026, 9, 9)))
        # Due Friday 2026-10-09, so the last standard run on or before it is Thursday the 8th.
        assert result.proposed_run_date == date(2026, 10, 8)

    def test_an_already_due_invoice_proposes_no_run(self) -> None:
        """Naming a date that cannot be met would be worse than saying it is due."""
        result = _assess(invoice=_invoice(invoice_date=date(2026, 7, 1)))
        assert result.proposed_run_date is None
        assert result.already_due

    def test_an_already_due_invoice_is_not_held(self) -> None:
        """§3: internal delay alone is not grounds for a manual payment, and the invoice is
        still payable. Blocking would hold a valid invoice and make it later."""
        result = _assess(invoice=_invoice(invoice_date=date(2026, 7, 1)))
        exception = next(
            e
            for e in result.exceptions
            if e.failed_rule == "payment_terms.standard_run_available_before_due_date"
        )
        assert not exception.blocking
        assert exception.owner.value == "ACCOUNTS_PAYABLE_MANAGER"

    def test_the_schedule_finding_says_it_is_a_proposal_only(self) -> None:
        """FIN-POL-006 §4 forbids an agent releasing a payment file."""
        result = _assess()
        finding = next(
            f for f in result.findings if f.rule == "scheduled_for_a_standard_payment_run"
        )
        assert "may not release" in finding.detail


class TestNoRunWithinTheTerms:
    """A future due date with no standard run before it is not the same as being overdue.

    The defect: the proposal returned ``None`` whenever no Tuesday or Thursday fell between
    today and the due date, and the code read that as "already due". A short term ending on a
    Friday or a Monday produces exactly that, so an invoice payable next Monday was reported as
    at or past its due date, with an exception steering the reader towards FIN-POL-006 §3's
    manual payment when §2's answer was simply the next run.
    """

    def test_a_future_due_date_with_no_run_before_it_is_not_already_due(self) -> None:
        # Processing on Friday 2026-09-11 with a two-day term: due Sunday, payable Friday the
        # 11th itself, and the only runs are Tuesday and Thursday.
        result = _assess(
            invoice=_invoice(invoice_date=date(2026, 9, 9)),
            purchase_order=_order(terms=2),
            printed_terms_days=2,
        )
        assert not result.already_due

    def test_the_proposal_is_the_next_standard_run_after_the_due_date(self) -> None:
        """Late is unavoidable here, so the soonest run is more useful than no answer."""
        result = _assess(
            invoice=_invoice(invoice_date=date(2026, 9, 9)),
            purchase_order=_order(terms=2),
            printed_terms_days=2,
        )
        assert result.proposed_run_date == date(2026, 9, 15)  # the following Tuesday
        assert not result.meets_agreed_terms

    def test_the_exception_states_the_lateness_rather_than_claiming_it_is_overdue(self) -> None:
        result = _assess(
            invoice=_invoice(invoice_date=date(2026, 9, 9)),
            purchase_order=_order(terms=2),
            printed_terms_days=2,
        )
        exception = next(
            e
            for e in result.exceptions
            if e.failed_rule == "payment_terms.standard_run_available_before_due_date"
        )
        assert "after the due date" in exception.observed
        assert not exception.blocking

    def test_a_genuinely_past_due_invoice_still_says_so(self) -> None:
        result = _assess(invoice=_invoice(invoice_date=date(2026, 7, 1)))
        assert result.already_due
        exception = next(
            e
            for e in result.exceptions
            if e.failed_rule == "payment_terms.standard_run_available_before_due_date"
        )
        assert "before the processing date" in exception.observed

    def test_a_normal_term_still_meets_the_agreed_terms(self) -> None:
        result = _assess()
        assert result.meets_agreed_terms
        assert result.proposed_run_date is not None
        assert result.proposed_run_date <= result.payable_on


class TestPrioritisation:
    """FIN-POL-007 §4: invoices due within two business days may be prioritised.

    The control was unimplemented because the due date was not computed. It is now, so the
    input exists. §4's own sentence limits what may follow: "urgency does not relax controls",
    so this orders a queue and changes nothing else.
    """

    def test_a_case_due_inside_the_window_may_be_prioritised(self) -> None:
        # Processing Friday 2026-09-11; a 1-day term from the 9th is payable Thursday the
        # 10th, which is inside the window.
        result = _assess(
            invoice=_invoice(invoice_date=date(2026, 9, 9)),
            purchase_order=_order(terms=1),
            printed_terms_days=1,
        )
        assert result.may_be_prioritised

    def test_a_case_due_well_ahead_may_not(self) -> None:
        result = _assess()
        assert not result.may_be_prioritised

    def test_the_window_is_measured_in_business_days(self) -> None:
        """Processing on a Friday, two business days reaches the following Tuesday."""
        # Invoice dated 2026-09-08 with a 7-day term is payable Tuesday 2026-09-15.
        result = _assess(
            invoice=_invoice(invoice_date=date(2026, 9, 8)),
            purchase_order=_order(terms=7),
            printed_terms_days=7,
        )
        assert result.payable_on == date(2026, 9, 15)
        assert result.may_be_prioritised

    def test_the_finding_says_controls_are_unchanged(self) -> None:
        """A reader must not take a priority flag for a relaxed threshold."""
        result = _assess(
            invoice=_invoice(invoice_date=date(2026, 9, 9)),
            purchase_order=_order(terms=1),
            printed_terms_days=1,
        )
        finding = next(
            f for f in result.findings if f.rule == "exception_review_may_be_prioritised"
        )
        assert "does not relax controls" in finding.detail
