"""Payment terms and the proposed payment run (FIN-POL-006 §1 to §3).

The request has carried ``payment_terms_days`` since the first version of the schema and
nothing read it. A controls review flagged that as more than dead weight: FIN-POL-006 §1 says
"an invoice's printed terms do not override the agreed terms", which makes the field on the
request a *claim by the supplier* that has to be compared with the order, not a value to be
used. Carrying it and never comparing it is the same failure the delegation scope had, where a
field was stored and never checked and therefore conferred something it should not.

So this module does three things, in the order the policy sets them out.

**It establishes the agreed terms.** The purchase order wins when it records them, because §1
gives a signed contract or order precedence. Otherwise the default is 30 calendar days. When
the invoice's printed terms disagree with the agreed terms, the disagreement is recorded and
the agreed terms are used.

**It computes the due date, then moves it off a non-business day.** §2 pays a weekend or
public-holiday due date on the *preceding* business day, not the following one, which matters:
the following business day would be late. Public holidays are out of scope here — the corpus
names them and supplies no calendar, and an invented calendar would produce authoritative
looking dates that are wrong. Only weekends are applied, and the limitation is recorded.

**It proposes a payment run.** §2 schedules approved invoices for "the next standard payment
run before their due date", and standard runs are Tuesday and Thursday. This module proposes
the *latest* qualifying run rather than the earliest, which is an interpretation: §3 requires a
documented commercial benefit for an early payment, so paying on the first available run
instead of the last one that still meets the terms would need a justification the case does not
carry. The reading is recorded here so it is not mistaken for the policy's wording.

Three cases, and telling them apart matters. Either a standard run falls between today and the
due date, or none does but the due date is still ahead, or the due date has passed. The middle
case gets the first run *after* the due date, with the lateness stated. An earlier version
collapsed the middle case into the last one and reported an invoice due next Monday as already
overdue.

Under §4 an agent may prepare a proposed schedule and may not release a payment file. This
module only ever returns dates. Nothing downstream of it can release anything, because no tool
in this system can move money.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field

from ap_agent.domain.enums import EscalationOwner, ExceptionCategory
from ap_agent.domain.evidence import Invoice, PurchaseOrder
from ap_agent.domain.results import Calculation, ExceptionRecord, PolicyFinding
from ap_agent.domain.rules._shared import (
    add_business_days,
    next_review_date,
    previous_business_day,
)

POLICY_TERMS = "FIN-POL-006 §1"
POLICY_SERVICE_LEVELS = "FIN-POL-007 §4"
POLICY_SCHEDULING = "FIN-POL-006 §2"
POLICY_MANUAL = "FIN-POL-006 §3"

#: FIN-POL-006 §1: "The default term is 30 calendar days from receipt of a valid invoice."
DEFAULT_TERMS_DAYS = 30

#: FIN-POL-006 §2: "Standard runs occur Tuesday and Thursday." Monday is 0.
STANDARD_RUN_WEEKDAYS = (1, 3)

#: FIN-POL-007 §4: "Invoices due within two business days may be prioritised, but urgency does
#: not relax controls." The window is in business days, so a Thursday case with a Monday due
#: date is inside it.
PRIORITY_WINDOW_BUSINESS_DAYS = 2


class PaymentTermsResult(BaseModel):
    """The agreed terms, the due date, and a proposed payment run."""

    model_config = ConfigDict(extra="forbid")

    agreed_terms_days: int
    terms_source: str
    due_date: date
    #: The due date after applying FIN-POL-006 §2's non-business-day rule.
    payable_on: date
    #: The next standard run that falls on or before ``payable_on`` and not in the past.
    proposed_run_date: date | None = None
    #: Whether the *due* date fell on a non-business day, before §2's adjustment. Named for
    #: what it is: the payment itself never settles on one, because ``payable_on`` has already
    #: been moved to the preceding business day. It is not a fraud signal, and a version that
    #: fed it to the weekend manual-payment indicator raised that indicator on roughly two
    #: invoices in seven for a condition §2 had already remedied.
    due_date_on_non_business_day: bool = False
    printed_terms_days: int | None = None
    printed_terms_conflict: bool = False
    #: Whether the proposed run meets the agreed terms. False when the only available run
    #: falls after the due date, which is a different condition from being past due.
    meets_agreed_terms: bool = True
    #: Whether the invoice was already payable before the processing date.
    already_due: bool = False
    #: FIN-POL-007 §4: whether the case falls in the window where an exception review may be
    #: prioritised. A queue-ordering hint and nothing more; §4 states in the same sentence
    #: that urgency does not relax controls, so no threshold or check varies with it.
    may_be_prioritised: bool = False
    #: Always empty today, and kept because the orchestrator reads the same four record
    #: lists from every rule result. Dates are not monetary calculations: the ``Calculation``
    #: model carries an amount and a rounding method, and an earlier version abused it to
    #: record a day count. The arithmetic is stated in the findings instead.
    calculations: list[Calculation] = Field(default_factory=list)
    exceptions: list[ExceptionRecord] = Field(default_factory=list)
    findings: list[PolicyFinding] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)


def _latest_run_by(*, payable_on: date, not_before: date) -> date | None:
    """The latest standard payment run in ``[not_before, payable_on]``, or ``None``.

    Searched backwards from the due date so the proposal is the last run that still meets the
    terms. Paying earlier than necessary without a documented commercial benefit would be an
    early payment under FIN-POL-006 §3 and needs its own approval; see the module docstring on
    why this is an interpretation of §2 rather than its wording.

    Terminates in at most seven steps: any seven consecutive days contain a Tuesday and a
    Thursday, so a window wider than a week always returns on its first or second candidate.
    """
    candidate = payable_on
    while candidate >= not_before:
        if candidate.weekday() in STANDARD_RUN_WEEKDAYS:
            return candidate
        candidate -= timedelta(days=1)
    return None


def _first_run_after(moment: date) -> date:
    """The earliest standard payment run strictly after ``moment``.

    Used when no run falls inside the terms. The invoice will be paid late whatever happens,
    so the useful answer is the soonest run rather than no answer at all: FIN-POL-006 §3 says
    internal delay is not grounds for a manual payment, which makes the next standard run the
    correct destination for a case in this position.
    """
    candidate = moment + timedelta(days=1)
    while candidate.weekday() not in STANDARD_RUN_WEEKDAYS:
        candidate += timedelta(days=1)
    return candidate


def assess_payment_terms(
    invoice: Invoice,
    *,
    purchase_order: PurchaseOrder | None,
    printed_terms_days: int | None,
    invoice_date_supplied: bool,
    as_of: datetime,
) -> PaymentTermsResult:
    """Establish the agreed terms and propose a payment run.

    ``printed_terms_days`` is the value the submission carried. It is passed separately from
    the invoice so that the comparison FIN-POL-006 §1 requires is explicit in the signature:
    one figure is a supplier claim, the other is the agreement.
    """
    review = next_review_date(as_of.date())
    calculations: list[Calculation] = []
    exceptions: list[ExceptionRecord] = []
    findings: list[PolicyFinding] = []
    assumptions: list[str] = []

    # ---- §1 the agreed terms -------------------------------------------------------------
    if purchase_order is not None and purchase_order.payment_terms_days is not None:
        agreed = purchase_order.payment_terms_days
        source = f"purchase order {purchase_order.po_reference}"
    else:
        agreed = DEFAULT_TERMS_DAYS
        source = f"{POLICY_TERMS} default of {DEFAULT_TERMS_DAYS} calendar days"

    conflict = printed_terms_days is not None and printed_terms_days != agreed
    if conflict:
        exceptions.append(
            ExceptionRecord(
                category=ExceptionCategory.OTHER_CONTROL_RISK,
                failed_rule="payment_terms.printed_terms_match_agreed_terms",
                expected=f"{agreed} days, from {source}",
                observed=f"{printed_terms_days} days printed on the invoice",
                owner=EscalationOwner.REQUESTER,
                policy_refs=[POLICY_TERMS],
                next_review_date=review,
                # Not blocking. FIN-POL-006 §1 already resolves the conflict by giving the
                # agreement precedence, so the case can proceed on the agreed terms. It is
                # recorded because a supplier unilaterally shortening terms on the document is
                # worth someone knowing about, and because a run that silently overrode the
                # invoice would leave no trace that it had.
                blocking=False,
                detail=(
                    "The agreed terms were applied. An invoice's printed terms do not override "
                    "the agreed terms under FIN-POL-006 §1, so the printed figure is recorded "
                    "as a discrepancy rather than used."
                ),
            )
        )
    findings.append(
        PolicyFinding(
            rule="agreed_payment_terms_applied",
            policy_ref=POLICY_TERMS,
            satisfied=not conflict,
            detail=(
                f"{agreed} days from {source}"
                + (
                    f"; the invoice printed {printed_terms_days} days, which does not override it"
                    if conflict
                    else ""
                )
            ),
        )
    )

    # ---- §1 and §2 the due date ----------------------------------------------------------
    if not invoice_date_supplied:
        assumptions.append(
            "No invoice date was supplied, so the substituted date was used as the receipt "
            "date for the payment-terms calculation. FIN-POL-006 §1 runs terms from receipt "
            "of a valid invoice, which this system does not capture separately."
        )
    else:
        assumptions.append(
            "The invoice date was used as the receipt date. FIN-POL-006 §1 runs terms from "
            "receipt of a valid invoice, and no separate receipt timestamp is captured."
        )

    due = invoice.invoice_date + timedelta(days=agreed)
    on_non_business_day = due.weekday() >= 5
    payable_on = previous_business_day(due) if on_non_business_day else due
    # Deliberately not recorded as a ``Calculation``. That model carries a monetary result
    # with a rounding method, and a due date is neither: an earlier version put the day count
    # in the amount field and recorded a rounding mode for a value that is never rounded. The
    # dates are on the result object, and the finding below states the arithmetic in words.
    findings.append(
        PolicyFinding(
            rule="due_date_computed_from_agreed_terms",
            policy_ref=POLICY_TERMS,
            satisfied=True,
            detail=(
                f"{invoice.invoice_date.isoformat()} plus {agreed} calendar days from {source} "
                f"gives {due.isoformat()}"
                + (
                    f", a non-business day, so payable {payable_on.isoformat()}, the preceding "
                    "business day under FIN-POL-006 §2"
                    if on_non_business_day
                    else ""
                )
                + ". Weekends only: the corpus references public holidays and supplies no "
                "calendar, so none is applied."
            ),
        )
    )

    # ---- §4 of FIN-POL-007: may this case jump the review queue? --------------------------
    # Recorded because the due date is now computed and the input therefore exists. It changes
    # nothing else: §4's own sentence is "Invoices due within two business days may be
    # prioritised, but urgency does not relax controls", so this affects the order a person
    # works through exceptions and no tolerance, limit or approval requirement anywhere.
    priority_cutoff = add_business_days(as_of.date(), PRIORITY_WINDOW_BUSINESS_DAYS)
    may_be_prioritised = payable_on <= priority_cutoff
    if may_be_prioritised:
        findings.append(
            PolicyFinding(
                rule="exception_review_may_be_prioritised",
                policy_ref=POLICY_SERVICE_LEVELS,
                satisfied=True,
                detail=(
                    f"payable {payable_on.isoformat()}, within "
                    f"{PRIORITY_WINDOW_BUSINESS_DAYS} business days of "
                    f"{as_of.date().isoformat()}. A queue-ordering hint only: FIN-POL-007 §4 "
                    "states that urgency does not relax controls, and no threshold or check "
                    "in this engine varies with it."
                ),
            )
        )

    # ---- §2 the proposed run -------------------------------------------------------------
    past_due = payable_on < as_of.date()
    proposed = None if past_due else _latest_run_by(payable_on=payable_on, not_before=as_of.date())
    if proposed is None and not past_due:
        # The due date is still ahead and no standard run falls before it. A short term
        # ending on a Friday or a Monday produces this. The invoice will be paid late, and
        # the soonest standard run is a more useful answer than none.
        late_run = _first_run_after(payable_on)
        days_late = (late_run - payable_on).days
        exceptions.append(
            ExceptionRecord(
                category=ExceptionCategory.OTHER_CONTROL_RISK,
                failed_rule="payment_terms.standard_run_available_before_due_date",
                expected=f"a Tuesday or Thursday run on or before {payable_on.isoformat()}",
                observed=(
                    f"the next standard run is {late_run.isoformat()}, {days_late} day(s) "
                    "after the due date"
                ),
                owner=EscalationOwner.ACCOUNTS_PAYABLE_MANAGER,
                policy_refs=[POLICY_SCHEDULING, POLICY_MANUAL],
                next_review_date=review,
                # Not blocking. The invoice is valid and payable; what is missing is a run
                # that meets the terms, which is a scheduling matter for Accounts Payable.
                blocking=False,
                detail=(
                    "No standard payment run falls between the processing date and the due "
                    "date, so the agreed terms cannot be met by a standard run. An early "
                    "payment needs a documented commercial benefit under FIN-POL-006 §3, and "
                    "a manual payment needs Treasury and Financial Control approval, so the "
                    "proposal is the next standard run."
                ),
            )
        )
        findings.append(
            PolicyFinding(
                rule="scheduled_for_a_standard_payment_run",
                policy_ref=POLICY_SCHEDULING,
                satisfied=False,
                detail=(
                    f"proposed run {late_run.isoformat()}, {days_late} day(s) after the due "
                    f"date {payable_on.isoformat()}: no standard run falls before it"
                ),
            )
        )
        return PaymentTermsResult(
            agreed_terms_days=agreed,
            terms_source=source,
            due_date=due,
            payable_on=payable_on,
            proposed_run_date=late_run,
            meets_agreed_terms=False,
            due_date_on_non_business_day=on_non_business_day,
            printed_terms_days=printed_terms_days,
            printed_terms_conflict=conflict,
            already_due=False,
            may_be_prioritised=may_be_prioritised,
            calculations=calculations,
            exceptions=exceptions,
            findings=findings,
            assumptions=assumptions,
        )

    already_due = past_due
    if proposed is not None:
        findings.append(
            PolicyFinding(
                rule="scheduled_for_a_standard_payment_run",
                policy_ref=POLICY_SCHEDULING,
                satisfied=True,
                detail=(
                    f"proposed run {proposed.isoformat()}, the last standard run on or before "
                    f"{payable_on.isoformat()}. Proposal only: under FIN-POL-006 §4 an agent "
                    "may prepare a schedule and may not release a payment file."
                ),
            )
        )
    else:
        exceptions.append(
            ExceptionRecord(
                category=ExceptionCategory.OTHER_CONTROL_RISK,
                failed_rule="payment_terms.standard_run_available_before_due_date",
                expected=f"a Tuesday or Thursday run on or before {payable_on.isoformat()}",
                observed=(
                    f"the invoice was payable {payable_on.isoformat()}, before the processing "
                    f"date {as_of.date().isoformat()}"
                ),
                owner=EscalationOwner.ACCOUNTS_PAYABLE_MANAGER,
                policy_refs=[POLICY_SCHEDULING, POLICY_MANUAL],
                next_review_date=review,
                # Not blocking. The invoice is payable; what is unavailable is a standard run
                # that still meets the terms. Blocking would hold a valid invoice and make it
                # later, and FIN-POL-006 §3 is explicit that internal delay is not itself a
                # reason for a manual payment.
                blocking=False,
                detail=(
                    "The invoice is at or past its due date, so no standard run can meet the "
                    "terms. A manual or same-day payment would require Treasury approval and "
                    "Financial Control co-approval under FIN-POL-006 §3; internal delay alone "
                    "is not sufficient grounds for one."
                ),
            )
        )
        findings.append(
            PolicyFinding(
                rule="scheduled_for_a_standard_payment_run",
                policy_ref=POLICY_SCHEDULING,
                satisfied=False,
                detail=(
                    f"payable {payable_on.isoformat()}, on or before the processing date "
                    f"{as_of.date().isoformat()}"
                ),
            )
        )

    return PaymentTermsResult(
        agreed_terms_days=agreed,
        terms_source=source,
        due_date=due,
        payable_on=payable_on,
        proposed_run_date=proposed,
        meets_agreed_terms=proposed is not None,
        due_date_on_non_business_day=on_non_business_day,
        printed_terms_days=printed_terms_days,
        printed_terms_conflict=conflict,
        already_due=already_due,
        may_be_prioritised=may_be_prioritised,
        calculations=calculations,
        exceptions=exceptions,
        findings=findings,
        assumptions=assumptions,
    )
