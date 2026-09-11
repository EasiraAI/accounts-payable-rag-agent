"""Separate assessment of tax (FIN-POL-002 §2).

FIN-POL-002 §2 ends with a sentence that reads like an aside and is in fact a control: "Tax,
rounding and foreign-exchange differences are assessed separately and must not be hidden
inside a price variance."

The matching engine already honours the second half. It compares net against net, so an order
recorded exclusive of tax is never contradicted by a tax-inclusive invoice total, and
FIN-POL-009 §3's rate movement is excluded from the price comparison rather than reported as
one. What was missing is the first half: the assessment. Tax was excluded from the comparison
and then never looked at again, so ``TAX_QUERY`` — one of the ten categories FIN-POL-007 §1
defines — could not be raised by any code path.

**What §2 does and does not say.** It requires a separate assessment. It does not define a
correctness test for tax, and nothing in the corpus states a rate. So this module is careful
about the difference between a fact and an expectation:

- That an invoice did not separate its tax is a *fact*, and a fact §2 speaks to directly.
  Without a separated tax the engine has no net, uses the gross, and any tax on the invoice
  turns up as a document variance against the order — reported to the requester as a pricing
  dispute, inviting a tolerance decision on a difference that is not a pricing difference.
  That is exactly what §2 forbids, and it is the query this module exists to raise.
- That the tax does not equal ten per cent of the net is an *expectation*, and the ten per
  cent is configuration. It is checked, because an overstated tax is worth a look, and the
  finding says plainly that no policy clause sets the rate.

**Nothing here blocks.** Every exception is raised with ``blocking=False``. A blocking
exception forces ``HOLD_FOR_INFORMATION`` (see ``rules/outcome.py``), and a review found the
first version of this module holding invoices on an assumed rate: a partly GST-free supply,
which §2 never prohibits, produced a tax below ten per cent and was held. A tax question
belongs on the case record and in the exception queue. It is not grounds on its own for
stopping a payment whose price, receipt, vendor and authority all check out.

**Only AUD, and only where freight is not in play.** Australian GST cannot arise on a
foreign-currency invoice, so the unseparated-tax attribution is skipped outside the policy
currency; FIN-POL-009 governs that case. Freight is excluded too, because FIN-POL-002 §2
allows a freight variance of up to AUD 75 on an order that permits it, and an unexplained
difference on such an order has a likelier explanation than tax.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from ap_agent.domain.enums import EscalationOwner, ExceptionCategory
from ap_agent.domain.evidence import Invoice, PurchaseOrder
from ap_agent.domain.money import quantize
from ap_agent.domain.results import Calculation, ExceptionRecord, PolicyFinding
from ap_agent.domain.rules._shared import POLICY_CURRENCY, next_review_date

POLICY_SEPARATE_ASSESSMENT = "FIN-POL-002 §2"
POLICY_CALCULATION = "FIN-POL-002 §5"
POLICY_CATEGORIES = "FIN-POL-007 §1"
POLICY_FX = "FIN-POL-009 §3"

#: The tax rate expected on a domestic invoice, as a percentage. Australian GST, matching the
#: ``jurisdiction: AU`` in the corpus front matter. **The policy states no rate.** This is an
#: environmental assumption, it is cited as one in every finding that uses it, and it is the
#: only value in this module a deployment in another jurisdiction has to change.
EXPECTED_TAX_RATE_PERCENT: Decimal = Decimal("10")

#: Tolerance on a tax comparison, in the invoice currency. Tax is computed per line and
#: rounded before the lines are totalled, so a document-level recomputation can differ from
#: the supplier's figure by a few cents without either being wrong.
TAX_ROUNDING_TOLERANCE: Decimal = Decimal("0.05")

_RATE_DISCLAIMER = (
    "No clause in the corpus states a tax rate; the expected figure comes from configuration "
    "and this comparison is informational."
)


class TaxResult(BaseModel):
    """The separate tax assessment FIN-POL-002 §2 requires."""

    model_config = ConfigDict(extra="forbid")

    assessed: bool = False
    tax_separated: bool = False
    expected_tax: Decimal | None = None
    calculations: list[Calculation] = Field(default_factory=list)
    exceptions: list[ExceptionRecord] = Field(default_factory=list)
    findings: list[PolicyFinding] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)


def _expected_tax(net: Decimal) -> Decimal:
    return quantize(net * EXPECTED_TAX_RATE_PERCENT / Decimal("100"))


def _finding(*, satisfied: bool, detail: str) -> PolicyFinding:
    return PolicyFinding(
        rule="tax_assessed_separately",
        policy_ref=POLICY_SEPARATE_ASSESSMENT,
        satisfied=satisfied,
        detail=detail,
    )


def _assess_separated(
    invoice: Invoice, *, review: date
) -> tuple[Decimal, list[Calculation], list[ExceptionRecord], list[PolicyFinding]]:
    """Assess an invoice that stated its own net and tax.

    Returns the expected tax alongside the records. Only an *overstatement* is queried. A tax
    below the configured rate is consistent with a supply that is partly GST-free, which the
    policy nowhere prohibits, and querying it would raise an exception on every mixed invoice.
    An overstatement has no such innocent reading available from the invoice alone.
    """
    expected = _expected_tax(invoice.net_amount)
    difference = quantize(invoice.tax_amount - expected)
    calculations = [
        Calculation(
            name="tax_expected_at_configured_rate",
            inputs={
                "invoice_net": str(invoice.net_amount),
                "rate_percent": str(EXPECTED_TAX_RATE_PERCENT),
                "invoice_tax": str(invoice.tax_amount),
            },
            formula="invoice_net x rate_percent / 100",
            result=expected,
            currency=invoice.currency,
            policy_ref=POLICY_CALCULATION,
            note=(
                "Assessed separately from the price comparison, as FIN-POL-002 §2 requires. "
                + _RATE_DISCLAIMER
            ),
        )
    ]
    exceptions: list[ExceptionRecord] = []
    findings: list[PolicyFinding] = []

    if invoice.tax_amount == 0:
        findings.append(
            _finding(
                satisfied=True,
                detail=(
                    "The invoice separates tax and declares none. Consistent with a GST-free "
                    "or export supply, and not treated as a discrepancy."
                ),
            )
        )
    elif difference > TAX_ROUNDING_TOLERANCE:
        exceptions.append(
            ExceptionRecord(
                category=ExceptionCategory.TAX_QUERY,
                failed_rule="tax.stated_tax_does_not_exceed_the_configured_rate",
                expected=(
                    f"no more than {expected} {invoice.currency}, being "
                    f"{EXPECTED_TAX_RATE_PERCENT}% of {invoice.net_amount} {invoice.currency}"
                ),
                observed=f"{invoice.tax_amount} {invoice.currency}",
                owner=EscalationOwner.FINANCIAL_CONTROL,
                policy_refs=[POLICY_SEPARATE_ASSESSMENT, POLICY_CATEGORIES],
                next_review_date=review,
                # Informational. FIN-POL-002 §2 requires tax to be assessed separately; it
                # sets no rate, and the corpus states none. Holding an otherwise clean invoice
                # on a configured expectation would be a control this system invented.
                blocking=False,
                detail=(
                    f"The stated tax exceeds the configured rate by {difference} "
                    f"{invoice.currency}. An understatement would be consistent with a partly "
                    f"GST-free supply; an overstatement is not. {_RATE_DISCLAIMER}"
                ),
            )
        )
        findings.append(
            _finding(
                satisfied=False,
                detail=(
                    f"stated tax {invoice.tax_amount} {invoice.currency} exceeds the expected "
                    f"{expected} {invoice.currency}"
                ),
            )
        )
    elif difference < -TAX_ROUNDING_TOLERANCE:
        findings.append(
            _finding(
                satisfied=True,
                detail=(
                    f"stated tax {invoice.tax_amount} {invoice.currency} is below "
                    f"{EXPECTED_TAX_RATE_PERCENT}% of the net ({expected} "
                    f"{invoice.currency}), which is consistent with a partly GST-free supply. "
                    "Recorded, not queried."
                ),
            )
        )
    else:
        findings.append(
            _finding(
                satisfied=True,
                detail=(
                    f"stated tax {invoice.tax_amount} {invoice.currency} agrees with "
                    f"{EXPECTED_TAX_RATE_PERCENT}% of the net within "
                    f"{TAX_ROUNDING_TOLERANCE} {invoice.currency}"
                ),
            )
        )
    return expected, calculations, exceptions, findings


def assess_tax(
    invoice: Invoice,
    *,
    tax_separated: bool,
    purchase_order: PurchaseOrder | None,
    as_of: datetime,
) -> TaxResult:
    """Assess the invoice's tax as its own question.

    ``tax_separated`` says whether the submission supplied components, rather than the engine
    having derived a zero tax from a gross-only request. The two are indistinguishable on the
    invoice object — both leave ``tax_amount`` at zero — and they mean opposite things: one is
    a supplier declaring an untaxed supply, the other is this system not knowing.
    """
    review = next_review_date(as_of.date())
    assumptions: list[str] = [
        f"The expected tax rate is taken as {EXPECTED_TAX_RATE_PERCENT}% (Australian GST, "
        "matching the corpus jurisdiction). The policy corpus states no rate, so this is a "
        "configured assumption about the environment and no finding derived from it blocks a "
        "case."
    ]

    if tax_separated:
        expected, calculations, exceptions, findings = _assess_separated(invoice, review=review)
        return TaxResult(
            assessed=True,
            tax_separated=True,
            expected_tax=expected,
            calculations=calculations,
            exceptions=exceptions,
            findings=findings,
            assumptions=assumptions,
        )

    # ---- the submission did not separate its tax ------------------------------------------
    assumptions.append(
        "The submission supplied no tax component, so the gross amount was used as the net "
        "for matching. Any tax on the invoice therefore appears as a document variance."
    )
    # Written inline rather than as a named flag so the ``is None`` narrows for the reader and
    # for the type checker: everything below this block has a purchase order.
    if (
        purchase_order is None
        or purchase_order.currency != invoice.currency
        or invoice.currency != POLICY_CURRENCY
    ):
        # Nothing to attribute. Either there is no comparable order, or the invoice is in a
        # currency in which Australian GST cannot arise — FIN-POL-009 §3 governs that case,
        # and applying a domestic rate to it would invent a tax that does not exist.
        return TaxResult(
            assessed=False,
            calculations=[],
            findings=[
                _finding(
                    satisfied=False,
                    detail=(
                        "The invoice does not separate tax, and the difference cannot be "
                        "attributed: there is no comparable order in the same currency, or the "
                        f"invoice is not in {POLICY_CURRENCY} (see {POLICY_FX})."
                    ),
                )
            ],
            assumptions=assumptions,
        )

    variance = quantize(invoice.net_amount - purchase_order.total_value)
    expected = _expected_tax(purchase_order.total_value)
    permits_freight = any(line.permits_freight for line in purchase_order.lines)
    calculations = [
        Calculation(
            name="unseparated_tax_attribution",
            inputs={
                "invoice_total": str(invoice.net_amount),
                "order_total": str(purchase_order.total_value),
                "expected_tax_on_order": str(expected),
                "rate_percent": str(EXPECTED_TAX_RATE_PERCENT),
            },
            formula="invoice_total - order_total, compared with order_total x rate_percent / 100",
            result=variance,
            currency=invoice.currency,
            policy_ref=POLICY_CALCULATION,
            note=(
                "Asks whether a document variance on a gross-only invoice can be attributed "
                "at all. FIN-POL-002 §2 forbids reporting tax as a price variance. "
                + _RATE_DISCLAIMER
            ),
        )
    ]
    exceptions = []
    findings = []

    if variance <= 0:
        findings.append(
            _finding(
                satisfied=True,
                detail=(
                    f"The invoice does not separate tax and does not exceed the order "
                    f"({variance} {invoice.currency}), so no part of the total is unattributed."
                ),
            )
        )
    elif permits_freight:
        findings.append(
            _finding(
                satisfied=False,
                detail=(
                    f"The invoice does not separate tax and exceeds the order by {variance} "
                    f"{invoice.currency}, but the order permits freight, which FIN-POL-002 §2 "
                    "allows to vary. The difference is left to the price comparison."
                ),
            )
        )
    else:
        consistent = abs(variance - expected) <= TAX_ROUNDING_TOLERANCE
        exceptions.append(
            ExceptionRecord(
                category=ExceptionCategory.TAX_QUERY,
                failed_rule="tax.invoice_separates_its_tax",
                expected="an invoice stating its net and tax components separately",
                observed=(
                    f"a gross amount of {invoice.gross_amount} {invoice.currency} with no tax "
                    f"component, exceeding the order by {variance} {invoice.currency}"
                ),
                owner=EscalationOwner.REQUESTER,
                policy_refs=[POLICY_SEPARATE_ASSESSMENT, POLICY_CATEGORIES],
                next_review_date=review,
                # Informational, but the difference it describes is not: an unattributed
                # amount also reaches the price comparison, which holds the case on its own
                # terms if it is outside tolerance. This record exists so the queue shows a
                # tax question rather than only a pricing dispute.
                blocking=False,
                detail=(
                    f"{variance} {invoice.currency} of the document total cannot be attributed "
                    "to a price, a quantity or a tax, because the invoice does not state its "
                    "components. FIN-POL-002 §2 requires tax to be assessed separately and "
                    "forbids it being hidden inside a price variance, and a tolerance decision "
                    "taken on this figure would be a decision about tax under the wrong "
                    "heading. "
                    + (
                        "The difference matches the configured rate on the order value, which "
                        "is consistent with unseparated tax. "
                        if consistent
                        else "The difference does not match the configured rate on the order "
                        "value, so the cause is not obviously tax. "
                    )
                    + "A corrected invoice showing net and tax resolves it."
                ),
            )
        )
        findings.append(
            _finding(
                satisfied=False,
                detail=(
                    f"{variance} {invoice.currency} unattributed on a gross-only invoice"
                    + (
                        f", consistent with tax at {EXPECTED_TAX_RATE_PERCENT}%"
                        if consistent
                        else ""
                    )
                ),
            )
        )

    # FIN-POL-002 §2 names three separate assessments. Rounding and foreign exchange are
    # recorded as applied, even when nothing is wrong, so a reviewer can see that the whole
    # sentence was honoured rather than only the part that produced an exception.
    findings.append(
        PolicyFinding(
            rule="rounding_and_fx_assessed_separately",
            policy_ref=POLICY_SEPARATE_ASSESSMENT,
            satisfied=True,
            detail=(
                f"Rounding is absorbed by the {TAX_ROUNDING_TOLERANCE} {invoice.currency} "
                "tolerance on the tax comparison and by the per-line tolerances in matching. "
                f"Currency differences are not converted: {POLICY_FX} holds a mismatched "
                "invoice rather than applying a rate, so no exchange difference enters a "
                "price variance."
            ),
        )
    )

    return TaxResult(
        assessed=True,
        tax_separated=False,
        expected_tax=expected,
        calculations=calculations,
        exceptions=exceptions,
        findings=findings,
        assumptions=assumptions,
    )
