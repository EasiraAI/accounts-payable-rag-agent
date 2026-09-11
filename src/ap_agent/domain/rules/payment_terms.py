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

**It proposes a payment run.** §2 schedules approved invoices for the next standard run
*before* the due date, and standard runs are Tuesday and Thursday. The proposal is the latest
such run that is not in the past and not after the due date. When no run remains, that is
itself the finding: the invoice is already due, and saying so is more useful than naming a
date that cannot be met.

Under §4 an agent may prepare a proposed schedule and may not release a payment file. This
module only ever returns dates. Nothing downstream of it can release anything, because no tool
in this system can move money.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from ap_agent.domain.enums import EscalationOwner, ExceptionCategory
from ap_agent.domain.evidence import Invoice, PurchaseOrder
from ap_agent.domain.results import Calculation, ExceptionRecord, PolicyFinding
from ap_agent.domain.rules._shared import next_review_date, previous_business_day

POLICY_TERMS = "FIN-POL-006 §1"
POLICY_SCHEDULING = "FIN-POL-006 §2"
POLICY_MANUAL = "FIN-POL-006 §3"

#: FIN-POL-006 §1: "The default term is 30 calendar days from receipt of a valid invoice."
DEFAULT_TERMS_DAYS = 30

#: FIN-POL-006 §2: "Standard runs occur Tuesday and Thursday." Monday is 0.
STANDARD_RUN_WEEKDAYS = (1, 3)


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
    #: Whether the computed due date fell on a non-business day. Read by the fraud assessment:
    #: FIN-POL-005 §3 treats a *weekend* manual-payment request as an indicator, and this is
    #: the observable fact that makes a manual request a weekend one.
    settlement_on_non_business_day: bool = False
    printed_terms_days: int | None = None
    printed_terms_conflict: bool = False
    already_due: bool = False
    calculations: list[Calculation] = Field(default_factory=list)
    exceptions: list[ExceptionRecord] = Field(default_factory=list)
    findings: list[PolicyFinding] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)


def _proposed_run_date(*, payable_on: date, not_before: date) -> date | None:
    """The latest standard payment run on or before ``payable_on``, not earlier than today.

    Searched backwards from the due date so the proposal is the last run that still meets the
    terms, which is the scheduling FIN-POL-006 §2 describes: the next run *before* the due
    date. Paying earlier than necessary without a documented commercial benefit would be an
    early payment under §3 and needs its own approval.
    """
    candidate = payable_on
    while candidate >= not_before:
        if candidate.weekday() in STANDARD_RUN_WEEKDAYS:
            return candidate
        candidate -= timedelta(days=1)
    return None


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
    calculations.append(
        Calculation(
            name="payment_due_date",
            inputs={
                "invoice_date": invoice.invoice_date.isoformat(),
                "agreed_terms_days": str(agreed),
                "terms_source": source,
            },
            formula="invoice_date + agreed_terms_days calendar days",
            # The term itself, in days. The calculation record carries a number, and the
            # dates it produces are in the note and on the result object; recording the term
            # here is what lets a reviewer check the arithmetic against the source.
            result=Decimal(agreed),
            currency=None,
            policy_ref=POLICY_TERMS,
            note=(
                f"Due {due.isoformat()}"
                + (
                    f"; a non-business day, so payable {payable_on.isoformat()}, the preceding "
                    "business day under FIN-POL-006 §2"
                    if on_non_business_day
                    else ""
                )
                + ". Weekends only: the corpus references public holidays and supplies no "
                "calendar, so none is applied."
            ),
        )
    )
    if on_non_business_day:
        findings.append(
            PolicyFinding(
                rule="non_business_day_due_date_brought_forward",
                policy_ref=POLICY_SCHEDULING,
                satisfied=True,
                detail=(
                    f"due {due.isoformat()} is a non-business day; payable "
                    f"{payable_on.isoformat()}, the preceding business day"
                ),
            )
        )

    # ---- §2 the proposed run -------------------------------------------------------------
    proposed = _proposed_run_date(payable_on=payable_on, not_before=as_of.date())
    already_due = proposed is None
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
                observed=f"no standard run remains after {as_of.date().isoformat()}",
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
        settlement_on_non_business_day=on_non_business_day,
        printed_terms_days=printed_terms_days,
        printed_terms_conflict=conflict,
        already_due=already_due,
        calculations=calculations,
        exceptions=exceptions,
        findings=findings,
        assumptions=assumptions,
    )
