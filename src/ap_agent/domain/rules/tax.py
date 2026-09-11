"""Separate assessment of tax (FIN-POL-002 §2).

FIN-POL-002 §2 ends with a sentence that is easy to read as an aside and is in fact a
control: "Tax, rounding and foreign-exchange differences are assessed separately and must not
be hidden inside a price variance."

The matching engine already honours half of it. It compares net against net, so a purchase
order recorded exclusive of tax is never contradicted by a tax-inclusive invoice total. What
was missing is the other half: the assessment. Tax was excluded from the comparison and then
never looked at again, so ``TAX_QUERY`` — one of the ten exception categories FIN-POL-007 §1
defines — could not be raised by any code path.

Two situations produce a query here.

**The invoice did not separate its tax.** When a submission carries a gross amount and no
components, the engine has no net to compare and uses the gross. Any tax on the invoice then
appears as a price variance against the order, which is precisely the outcome §2 forbids: the
exception would name the wrong cause, go to the requester as a pricing dispute, and invite a
tolerance decision on a difference that is not a pricing difference at all. This module
recognises the shape of that difference. When the unexplained variance matches the tax that
the configured rate would produce on the order value, it says so.

**The separated tax is neither zero nor the expected rate.** A stated tax that is some third
figure is a disagreement with the supplier about tax treatment, and FIN-POL-007 §1 has a
category for exactly that. Zero is left alone deliberately: GST-free and export supplies
exist, and FIN-POL-009 governs the foreign-currency case, so treating a zero as an error would
raise a query on every legitimately untaxed invoice.

**The rate is configuration, not policy.** The corpus states a jurisdiction (AU) and never
states a rate. The value below is therefore an assumption about the environment rather than a
rule read out of the policy, and every finding this module produces names it. A deployment in
another jurisdiction changes the constant; nothing else in the module depends on its value.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from ap_agent.domain.enums import EscalationOwner, ExceptionCategory
from ap_agent.domain.evidence import Invoice, PurchaseOrder
from ap_agent.domain.money import quantize
from ap_agent.domain.results import Calculation, ExceptionRecord, PolicyFinding
from ap_agent.domain.rules._shared import next_review_date

POLICY_SEPARATE_ASSESSMENT = "FIN-POL-002 §2"
POLICY_CALCULATION = "FIN-POL-002 §5"
POLICY_CATEGORIES = "FIN-POL-007 §1"

#: The tax rate expected on a domestic invoice, as a percentage. Australian GST, matching the
#: ``jurisdiction: AU`` recorded in the corpus front matter. The policy text states no rate,
#: so this is an environmental assumption and is cited as one wherever it is applied.
EXPECTED_TAX_RATE_PERCENT: Decimal = Decimal("10")

#: Tolerance on a tax comparison, in the invoice currency. Tax is computed per line and
#: rounded before the lines are totalled, so a document-level recomputation can differ from
#: the supplier's figure by a few cents without either being wrong.
TAX_ROUNDING_TOLERANCE: Decimal = Decimal("0.05")


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
    a supplier declaring no tax, the other is this system not knowing.
    """
    review = next_review_date(as_of.date())
    calculations: list[Calculation] = []
    exceptions: list[ExceptionRecord] = []
    findings: list[PolicyFinding] = []
    assumptions: list[str] = [
        f"The expected tax rate is taken as {EXPECTED_TAX_RATE_PERCENT}% (Australian GST, "
        "matching the corpus jurisdiction). No rate is stated in the policy corpus, so this "
        "is a configured assumption about the environment, not a rule read from policy."
    ]

    if tax_separated:
        expected = _expected_tax(invoice.net_amount)
        difference = quantize(invoice.tax_amount - expected)
        calculations.append(
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
                    "Assessed separately from the price comparison, as FIN-POL-002 §2 "
                    "requires. The rate is configuration; see the recorded assumption."
                ),
            )
        )
        if invoice.tax_amount == 0:
            # Left alone on purpose. GST-free and export supplies exist, and FIN-POL-009
            # governs foreign-currency invoices. Querying every zero would raise a tax
            # exception on every legitimately untaxed invoice, which is noise, and noise in a
            # control queue is how real exceptions get cleared without being read.
            findings.append(
                PolicyFinding(
                    rule="tax_assessed_separately",
                    policy_ref=POLICY_SEPARATE_ASSESSMENT,
                    satisfied=True,
                    detail=(
                        "The invoice separates tax and declares none. Consistent with a "
                        "GST-free or export supply; not treated as a discrepancy."
                    ),
                )
            )
        elif abs(difference) > TAX_ROUNDING_TOLERANCE:
            exceptions.append(
                ExceptionRecord(
                    category=ExceptionCategory.TAX_QUERY,
                    failed_rule="tax.stated_tax_matches_expected_rate",
                    expected=(
                        f"{expected} {invoice.currency} at {EXPECTED_TAX_RATE_PERCENT}% of "
                        f"{invoice.net_amount} {invoice.currency}"
                    ),
                    observed=f"{invoice.tax_amount} {invoice.currency}",
                    owner=EscalationOwner.FINANCIAL_CONTROL,
                    policy_refs=[POLICY_SEPARATE_ASSESSMENT, POLICY_CATEGORIES],
                    next_review_date=review,
                    detail=(
                        f"The stated tax differs from the configured rate by {difference} "
                        f"{invoice.currency}. It is neither zero, which would indicate an "
                        "untaxed supply, nor the expected amount, so the tax treatment is in "
                        "question and is assessed separately from any price variance."
                    ),
                )
            )
            findings.append(
                PolicyFinding(
                    rule="tax_assessed_separately",
                    policy_ref=POLICY_SEPARATE_ASSESSMENT,
                    satisfied=False,
                    detail=(
                        f"stated tax {invoice.tax_amount} {invoice.currency} against expected "
                        f"{expected} {invoice.currency}"
                    ),
                )
            )
        else:
            findings.append(
                PolicyFinding(
                    rule="tax_assessed_separately",
                    policy_ref=POLICY_SEPARATE_ASSESSMENT,
                    satisfied=True,
                    detail=(
                        f"stated tax {invoice.tax_amount} {invoice.currency} agrees with "
                        f"{EXPECTED_TAX_RATE_PERCENT}% of the net within "
                        f"{TAX_ROUNDING_TOLERANCE} {invoice.currency}"
                    ),
                )
            )
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
    if purchase_order is None or purchase_order.currency != invoice.currency:
        # Without a comparable order there is no variance to explain, so there is nothing to
        # attribute to tax. The unknown is still recorded by the caller.
        findings.append(
            PolicyFinding(
                rule="tax_assessed_separately",
                policy_ref=POLICY_SEPARATE_ASSESSMENT,
                satisfied=False,
                detail=(
                    "The invoice does not separate tax and no comparable order is available, "
                    "so tax cannot be assessed apart from the total."
                ),
            )
        )
        return TaxResult(
            assessed=False,
            calculations=calculations,
            findings=findings,
            assumptions=assumptions,
        )

    variance = quantize(invoice.net_amount - purchase_order.total_value)
    expected = _expected_tax(purchase_order.total_value)
    calculations.append(
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
                "Asks whether an unexplained document variance is in fact the tax the invoice "
                "did not separate. FIN-POL-002 §2 forbids reporting tax as a price variance."
            ),
        )
    )
    if abs(variance - expected) <= TAX_ROUNDING_TOLERANCE and expected > 0:
        exceptions.append(
            ExceptionRecord(
                category=ExceptionCategory.TAX_QUERY,
                failed_rule="tax.invoice_separates_its_tax",
                expected="an invoice stating its net and tax components separately",
                observed=(
                    f"a gross amount of {invoice.gross_amount} {invoice.currency} with no tax "
                    "component"
                ),
                owner=EscalationOwner.REQUESTER,
                policy_refs=[POLICY_SEPARATE_ASSESSMENT, POLICY_CATEGORIES],
                next_review_date=review,
                detail=(
                    f"The document exceeds the order by {variance} {invoice.currency}, which "
                    f"matches the {EXPECTED_TAX_RATE_PERCENT}% tax on the order value of "
                    f"{purchase_order.total_value} {invoice.currency}. The difference is "
                    "recorded as a tax question rather than a price variance: the price may "
                    "well be correct, and a tolerance decision on this figure would be a "
                    "decision about tax taken under the wrong heading."
                ),
            )
        )
        findings.append(
            PolicyFinding(
                rule="tax_assessed_separately",
                policy_ref=POLICY_SEPARATE_ASSESSMENT,
                satisfied=False,
                detail=(
                    f"variance {variance} {invoice.currency} is consistent with unseparated "
                    f"tax at {EXPECTED_TAX_RATE_PERCENT}%"
                ),
            )
        )
    else:
        findings.append(
            PolicyFinding(
                rule="tax_assessed_separately",
                policy_ref=POLICY_SEPARATE_ASSESSMENT,
                satisfied=False,
                detail=(
                    f"The invoice does not separate tax. The document variance of {variance} "
                    f"{invoice.currency} is not consistent with tax at "
                    f"{EXPECTED_TAX_RATE_PERCENT}% on the order value, so it is left to the "
                    "price comparison."
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
