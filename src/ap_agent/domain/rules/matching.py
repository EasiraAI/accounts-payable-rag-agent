"""Three-way matching and tolerances (FIN-POL-002).

The function in this module is pure: same inputs, same outputs, no clock of its own, no I/O.
That is what allows the thresholds to be tested at their boundaries and what makes a run
reproducible from its persisted evidence.

Three decisions in the implementation are worth stating, because each rules out a plausible
alternative.

**Price variance is measured against the invoiced quantity, not the ordered quantity.**
Expected value is ``invoiced_quantity x po_unit_price``. Measuring against the ordered
quantity would report a variance on every legitimate partial delivery, and the quantity
question is assessed separately against the receipt. This keeps a price disagreement
distinguishable from a delivery shortfall, which FIN-POL-002 §3 requires when it demands the
failed line, expected value, actual value and difference.

**The document total is checked against its own limit, not the sum of the line limits.**
Summing the line limits would let a variance be divided across lines until each slice fits,
which is the in-document form of the behaviour FIN-POL-002 §3 prohibits. The document limit
uses the same formula as a goods line, applied to the purchase-order total.

**Tax is excluded from the comparison.** Net against net. FIN-POL-002 §2 requires tax,
rounding and foreign-exchange differences to be assessed separately and not hidden inside a
price variance, so including tax here would defeat the control it is meant to support.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from ap_agent.domain.enums import (
    EscalationOwner,
    ExceptionCategory,
    LineType,
)
from ap_agent.domain.evidence import Invoice, InvoiceLine, POLine, PurchaseOrder
from ap_agent.domain.money import Money, minimum, percent_of, quantize
from ap_agent.domain.results import Calculation, ExceptionRecord, PolicyFinding, Unknown
from ap_agent.domain.rules._shared import POLICY_CURRENCY, next_review_date, threshold

# ---- policy constants (FIN-POL-002 §2) -----------------------------------------------

GOODS_ABSOLUTE_LIMIT = "50.00"
GOODS_PERCENT_LIMIT = Decimal("1")
SERVICE_ABSOLUTE_LIMIT = "100.00"
SERVICE_PERCENT_LIMIT = Decimal("2")
FREIGHT_ABSOLUTE_LIMIT = "75.00"

POLICY_TOLERANCE = "FIN-POL-002 §2"
POLICY_MATCH_BASIS = "FIN-POL-002 §1"
POLICY_OUTCOMES = "FIN-POL-002 §3"
POLICY_MISSING_RECEIPT = "FIN-POL-002 §4"
POLICY_CALCULATION = "FIN-POL-002 §5"
POLICY_FX = "FIN-POL-009 §3"
POLICY_NON_PO = "FIN-POL-012 §1"


class LineMatch(BaseModel):
    """The result of matching one invoice line."""

    model_config = ConfigDict(extra="forbid")

    invoice_line_number: int
    po_line_number: int | None
    line_type: LineType | None
    expected_value: Decimal | None
    invoiced_value: Decimal
    variance: Decimal | None
    tolerance_limit: Decimal | None
    quantity_invoiced: Decimal
    quantity_received: Decimal | None
    within_tolerance: bool
    matched: bool


class MatchResult(BaseModel):
    """Everything the matching control produced.

    Exceptions, calculations, findings, assumptions and unknowns are all returned rather
    than raised, because a run must be able to report several independent control failures
    at once. An exception-raising design would report only the first.
    """

    model_config = ConfigDict(extra="forbid")

    po_present: bool
    receipt_present: bool
    line_level: bool
    all_within_tolerance: bool
    currency_consistent: bool
    line_matches: list[LineMatch] = Field(default_factory=list)
    calculations: list[Calculation] = Field(default_factory=list)
    exceptions: list[ExceptionRecord] = Field(default_factory=list)
    findings: list[PolicyFinding] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    unknowns: list[Unknown] = Field(default_factory=list)


def _tolerance_limit_for(po_line: POLine, currency: str) -> tuple[Money, str]:
    """The tolerance band for a line, with the formula used to derive it."""
    line_value = Money(amount=po_line.line_value, currency=currency)
    if po_line.line_type is LineType.SERVICE:
        absolute = threshold(SERVICE_ABSOLUTE_LIMIT, currency)
        percentage = percent_of(line_value, SERVICE_PERCENT_LIMIT)
        return minimum(absolute, percentage), f"min({SERVICE_ABSOLUTE_LIMIT}, 2% of po_line_value)"
    if po_line.line_type is LineType.FREIGHT:
        if po_line.permits_freight:
            return threshold(
                FREIGHT_ABSOLUTE_LIMIT, currency
            ), f"{FREIGHT_ABSOLUTE_LIMIT} (PO permits freight)"
        # No permission means no allowance, not a default allowance.
        return Money.zero(currency), "0.00 (PO does not permit freight)"
    absolute = threshold(GOODS_ABSOLUTE_LIMIT, currency)
    percentage = percent_of(line_value, GOODS_PERCENT_LIMIT)
    return minimum(absolute, percentage), f"min({GOODS_ABSOLUTE_LIMIT}, 1% of po_line_value)"


def _document_tolerance_limit(po: PurchaseOrder, currency: str) -> tuple[Money, str]:
    """The tolerance band applied to the document total.

    The limit is the **widest single-line allowance** on the order, not the sum of the line
    allowances and not the goods band applied blindly.

    Summing the line allowances is wrong because it grows with the number of lines: a
    variance could be divided across lines until every slice fits and the total still passed,
    which is the in-document form of the splitting behaviour FIN-POL-002 §3 prohibits.

    Applying the goods band regardless of composition is also wrong: it would reject a
    service invoice whose single line is legitimately within the wider service band that
    FIN-POL-002 §2 grants it, so the document check would contradict the line check.

    Taking the maximum satisfies both. A document may vary by as much as its most permissive
    line permits, and no more, so adding lines never increases the total allowance.
    """
    if not po.lines:
        limit = minimum(
            threshold(GOODS_ABSOLUTE_LIMIT, currency),
            percent_of(Money(amount=po.total_value, currency=currency), GOODS_PERCENT_LIMIT),
        )
        return limit, f"min({GOODS_ABSOLUTE_LIMIT}, 1% of po_total)"
    limits = [_tolerance_limit_for(line, currency)[0] for line in po.lines]
    widest = max(limits, key=lambda value: value.amount)
    return widest, "max(per-line tolerance limits on the order)"


def _line_has_receipt_evidence(po: PurchaseOrder, po_line: POLine) -> tuple[bool, Decimal]:
    """Whether a line's delivery is evidenced, and the receipted quantity.

    For services, a service owner's confirmation of completion stands in for a goods
    receipt: FIN-POL-002 §2 makes that confirmation the condition for the service tolerance,
    and there is nothing physical to receipt.
    """
    received = po.quantity_received_for(po_line.line_number)
    if received > 0:
        return True, received
    if po_line.line_type is LineType.SERVICE and po_line.service_completion_confirmed:
        return True, po_line.quantity_ordered
    return False, received


def _missing_po_exception(invoice: Invoice, review: date) -> ExceptionRecord:
    if invoice.po_reference:
        observed = (
            f"invoice cites purchase order {invoice.po_reference} but no such order was "
            "returned by the purchasing system"
        )
        refs = [POLICY_MATCH_BASIS, POLICY_OUTCOMES]
    else:
        observed = "invoice carries no purchase-order reference and no non-PO justification"
        refs = [POLICY_MATCH_BASIS, POLICY_NON_PO]
    return ExceptionRecord(
        category=ExceptionCategory.MISSING_PO,
        failed_rule="three_way_match.purchase_order_present",
        expected="an approved purchase order to match the invoice against",
        observed=observed,
        owner=EscalationOwner.REQUESTER,
        policy_refs=refs,
        next_review_date=review,
        detail=(
            "Matching cannot be performed without the order. Under FIN-POL-012 §1 a missing "
            "purchase order is not automatically an emergency; non-PO processing is limited "
            "to the categories listed there."
        ),
    )


def three_way_match(  # one control expressed as one readable pass
    invoice: Invoice,
    po: PurchaseOrder | None,
    *,
    as_of: datetime,
) -> MatchResult:
    """Match an invoice against its purchase order and recorded receipts."""
    currency = invoice.currency
    review = next_review_date(as_of.date())
    calculations: list[Calculation] = []
    exceptions: list[ExceptionRecord] = []
    findings: list[PolicyFinding] = []
    assumptions: list[str] = []
    unknowns: list[Unknown] = []
    line_matches: list[LineMatch] = []

    if currency != POLICY_CURRENCY:
        assumptions.append(
            f"Policy tolerance limits are stated in {POLICY_CURRENCY}; they have been applied "
            f"numerically in {currency} because converting a threshold requires a cited rate "
            f"under {POLICY_FX} and no rate source is configured."
        )

    # ---- purchase order present -----------------------------------------------------
    if po is None:
        exceptions.append(_missing_po_exception(invoice, review))
        findings.append(
            PolicyFinding(
                rule="purchase_order_present",
                policy_ref=POLICY_MATCH_BASIS,
                satisfied=False,
                detail="no purchase order available, so no line or total matching was performed",
            )
        )
        unknowns.append(
            Unknown(
                item="purchase-order lines, totals, tolerances and receipts",
                reason="the purchasing system returned no order for this invoice",
                impact=(
                    "three-way matching could not be performed, so the invoice cannot be "
                    "approved for posting"
                ),
                how_to_resolve=(
                    "supply the purchase-order reference, or process as a non-PO invoice with "
                    "the justification FIN-POL-012 §3 requires"
                ),
                source_attempted="get_purchase_order",
            )
        )
        return MatchResult(
            po_present=False,
            receipt_present=False,
            line_level=False,
            all_within_tolerance=False,
            currency_consistent=False,
            calculations=calculations,
            exceptions=exceptions,
            findings=findings,
            assumptions=assumptions,
            unknowns=unknowns,
        )

    # ---- purchase order approved ----------------------------------------------------
    if po.is_approved:
        findings.append(
            PolicyFinding(
                rule="purchase_order_approved",
                policy_ref=POLICY_MATCH_BASIS,
                satisfied=True,
                detail=f"purchase order {po.po_reference} is approved",
            )
        )
    else:
        exceptions.append(
            ExceptionRecord(
                category=ExceptionCategory.OTHER_CONTROL_RISK,
                failed_rule="three_way_match.purchase_order_approved",
                expected="purchase order in status APPROVED",
                observed=f"purchase order {po.po_reference} is in status {po.approval_status}",
                owner=EscalationOwner.REQUESTER,
                policy_refs=[POLICY_MATCH_BASIS],
                next_review_date=review,
                detail="An unapproved order is not a valid matching basis.",
            )
        )
        findings.append(
            PolicyFinding(
                rule="purchase_order_approved",
                policy_ref=POLICY_MATCH_BASIS,
                satisfied=False,
                detail=f"status {po.approval_status}",
            )
        )

    # ---- currency agreement ---------------------------------------------------------
    currency_consistent = invoice.currency == po.currency
    if currency_consistent:
        findings.append(
            PolicyFinding(
                rule="currency_matches_purchase_order",
                policy_ref=POLICY_MATCH_BASIS,
                satisfied=True,
                detail=f"invoice and order are both in {currency}",
            )
        )
    else:
        # Amount comparison is skipped entirely rather than performed on incomparable
        # numbers. FIN-POL-009 §2 forbids inventing a rate, and comparing 100 USD to
        # 100 AUD as though the figures were equivalent would report a variance of zero.
        exceptions.append(
            ExceptionRecord(
                category=ExceptionCategory.OTHER_CONTROL_RISK,
                failed_rule="three_way_match.currency_agreement",
                expected=f"invoice currency {po.currency} to match the purchase order",
                observed=f"invoice is in {invoice.currency}, order is in {po.currency}",
                owner=EscalationOwner.TREASURY,
                policy_refs=[POLICY_MATCH_BASIS, POLICY_FX],
                next_review_date=review,
                blocking=not po.permits_currency_conversion,
                detail=(
                    "The order explicitly permits conversion, so the case is recorded for "
                    "Treasury review rather than blocked."
                    if po.permits_currency_conversion
                    else "A supplier cannot change currency through invoice text alone."
                ),
            )
        )
        findings.append(
            PolicyFinding(
                rule="currency_matches_purchase_order",
                policy_ref=POLICY_FX,
                satisfied=False,
                detail=f"invoice {invoice.currency} against order {po.currency}",
            )
        )
        unknowns.append(
            Unknown(
                item=f"the {po.currency} equivalent of the invoice amount",
                reason="no corporate daily rate source is configured",
                impact="tolerance and authority cannot be assessed in the order currency",
                how_to_resolve=(
                    "supply the corporate daily rate for the invoice date, with its source, "
                    "per FIN-POL-009 §2"
                ),
            )
        )

    # ---- receipts present at all ----------------------------------------------------
    if not po.has_any_receipt():
        service_lines_confirmed = [
            line
            for line in po.lines
            if line.line_type is LineType.SERVICE and line.service_completion_confirmed
        ]
        if not service_lines_confirmed:
            exceptions.append(
                ExceptionRecord(
                    category=ExceptionCategory.MISSING_RECEIPT,
                    failed_rule="three_way_match.receipt_recorded",
                    expected=f"a recorded goods or service receipt against {po.po_reference}",
                    observed="the receipting system returned no receipt for this order",
                    owner=EscalationOwner.RECEIPTER,
                    policy_refs=[POLICY_MISSING_RECEIPT],
                    next_review_date=review,
                    detail=(
                        "Receipt must not be inferred from delivery language on the invoice. "
                        "The designated receipter can confirm delivery, after which the case "
                        "resumes from this control."
                    ),
                )
            )
            unknowns.append(
                Unknown(
                    item="evidence that goods or services were received",
                    reason="no receipt record exists for the purchase order",
                    impact="the invoice cannot be approved for payment",
                    how_to_resolve="ask the designated receipter to record the receipt",
                    source_attempted="get_purchase_order.receipts",
                )
            )

    line_level = bool(invoice.lines) and currency_consistent
    if not invoice.lines:
        assumptions.append(
            "The request carried no invoice lines, so matching was performed on document "
            "totals only. FIN-POL-002 §1 requires per-line matching as well; the line-level "
            "control is recorded as not performed rather than as passed."
        )
        unknowns.append(
            Unknown(
                item="invoice line detail",
                reason="the processing request supplied no lines",
                impact=(
                    "per-line price and quantity variances were not assessed; only the "
                    "document total was compared"
                ),
                how_to_resolve="supply invoice lines with their purchase-order line references",
            )
        )
    elif not currency_consistent:
        assumptions.append(
            "Line-level matching was not attempted because the invoice and order are in "
            "different currencies."
        )

    matched_expected_total = Money.zero(currency)
    any_line_unmatched = False

    # ---- per-line matching ----------------------------------------------------------
    if line_level:
        for line in invoice.lines:
            po_line = po.line(line.po_line_number) if line.po_line_number is not None else None
            if po_line is None:
                any_line_unmatched = True
                line_matches.append(
                    LineMatch(
                        invoice_line_number=line.line_number,
                        po_line_number=line.po_line_number,
                        line_type=None,
                        expected_value=None,
                        invoiced_value=line.line_total,
                        variance=None,
                        tolerance_limit=None,
                        quantity_invoiced=line.quantity,
                        quantity_received=None,
                        within_tolerance=False,
                        matched=False,
                    )
                )
                exceptions.append(
                    ExceptionRecord(
                        category=ExceptionCategory.MISSING_PO,
                        failed_rule="three_way_match.line_maps_to_purchase_order_line",
                        expected=(
                            f"invoice line {line.line_number} to reference a line on "
                            f"{po.po_reference}"
                        ),
                        observed=(
                            f"invoice line {line.line_number} references purchase-order line "
                            f"{line.po_line_number}, which does not exist on {po.po_reference}"
                        ),
                        owner=EscalationOwner.REQUESTER,
                        policy_refs=[POLICY_MATCH_BASIS],
                        next_review_date=review,
                        detail="An unmapped line cannot be matched, priced or receipted.",
                    )
                )
                continue

            line_match, line_calcs, line_exceptions = _match_line(
                line=line,
                po_line=po_line,
                po=po,
                currency=currency,
                review=review,
            )
            line_matches.append(line_match)
            calculations.extend(line_calcs)
            exceptions.extend(line_exceptions)
            if line_match.expected_value is not None:
                matched_expected_total = matched_expected_total + Money(
                    amount=line_match.expected_value, currency=currency
                )

    # ---- document total -------------------------------------------------------------
    if currency_consistent:
        if line_level and any_line_unmatched:
            unknowns.append(
                Unknown(
                    item="document total variance",
                    reason="at least one invoice line could not be mapped to a purchase-order line",
                    impact=(
                        "the document total was not compared, because the expected total is "
                        "incomplete"
                    ),
                    how_to_resolve="correct the purchase-order line references on the invoice",
                )
            )
        else:
            expected_total = (
                matched_expected_total
                if line_level
                else Money(amount=po.total_value, currency=currency)
            )
            document_limit, document_limit_formula = _document_tolerance_limit(po, currency)
            document_variance = quantize(invoice.net_amount - expected_total.amount)
            calculations.append(
                Calculation(
                    name="document_total_variance",
                    inputs={
                        "invoice_net": str(invoice.net_amount),
                        "expected_net": str(expected_total.amount),
                        "basis": "sum of matched line expected values"
                        if line_level
                        else "purchase-order total",
                    },
                    formula="invoice_net - expected_net",
                    result=document_variance,
                    currency=currency,
                    policy_ref=POLICY_CALCULATION,
                    note="Net of tax: FIN-POL-002 §2 assesses tax separately.",
                )
            )
            calculations.append(
                Calculation(
                    name="document_tolerance_limit",
                    inputs={
                        "po_total": str(po.total_value),
                        "line_limits": ", ".join(
                            f"line {line.line_number}: "
                            f"{_tolerance_limit_for(line, currency)[0].amount}"
                            for line in po.lines
                        ),
                    },
                    formula=document_limit_formula,
                    result=document_limit.amount,
                    currency=currency,
                    policy_ref=POLICY_TOLERANCE,
                    note=(
                        "The widest single-line allowance on the order, not the sum of the line "
                        "allowances. Summing would let a variance be divided across lines until "
                        "each slice fits, which FIN-POL-002 §3 prohibits."
                    ),
                )
            )
            within = abs(document_variance) <= document_limit.amount
            if within:
                findings.append(
                    PolicyFinding(
                        rule="document_total_within_tolerance",
                        policy_ref=POLICY_TOLERANCE,
                        satisfied=True,
                        detail=(
                            f"variance {document_variance} {currency} against limit "
                            f"{document_limit.amount} {currency}"
                        ),
                    )
                )
            else:
                exceptions.append(
                    ExceptionRecord(
                        category=ExceptionCategory.PRICE_VARIANCE,
                        failed_rule="three_way_match.document_total_within_tolerance",
                        expected=f"document net of {expected_total.amount} {currency}",
                        observed=f"invoice net of {invoice.net_amount} {currency}",
                        owner=EscalationOwner.REQUESTER,
                        policy_refs=[POLICY_TOLERANCE, POLICY_OUTCOMES],
                        next_review_date=review,
                        detail=(
                            f"Difference {document_variance} {currency} exceeds the applicable "
                            f"threshold of {document_limit.amount} {currency}."
                        ),
                    )
                )
                findings.append(
                    PolicyFinding(
                        rule="document_total_within_tolerance",
                        policy_ref=POLICY_TOLERANCE,
                        satisfied=False,
                        detail=(
                            f"variance {document_variance} {currency} exceeds "
                            f"{document_limit.amount}"
                        ),
                    )
                )

    receipt_present = not any(
        exception.category is ExceptionCategory.MISSING_RECEIPT for exception in exceptions
    )
    if receipt_present and po.has_any_receipt():
        findings.append(
            PolicyFinding(
                rule="receipt_recorded",
                policy_ref=POLICY_MISSING_RECEIPT,
                satisfied=True,
                detail=f"{len(po.receipts)} receipt record(s) against {po.po_reference}",
            )
        )

    all_within_tolerance = not any(exception.blocking for exception in exceptions)

    return MatchResult(
        po_present=True,
        receipt_present=receipt_present,
        line_level=line_level,
        all_within_tolerance=all_within_tolerance,
        currency_consistent=currency_consistent,
        line_matches=line_matches,
        calculations=calculations,
        exceptions=exceptions,
        findings=findings,
        assumptions=assumptions,
        unknowns=unknowns,
    )


def _match_line(
    *,
    line: InvoiceLine,
    po_line: POLine,
    po: PurchaseOrder,
    currency: str,
    review: date,
) -> tuple[LineMatch, list[Calculation], list[ExceptionRecord]]:
    """Match one invoice line. Returns the outcome plus its calculations and exceptions."""
    calculations: list[Calculation] = []
    exceptions: list[ExceptionRecord] = []
    prefix = f"line_{line.line_number}"

    expected_value = quantize(line.quantity * po_line.unit_price)
    variance = quantize(line.line_total - expected_value)
    limit, limit_formula = _tolerance_limit_for(po_line, currency)

    calculations.append(
        Calculation(
            name=f"{prefix}_expected_value",
            inputs={
                "quantity_invoiced": str(line.quantity),
                "po_unit_price": str(po_line.unit_price),
            },
            formula="quantity_invoiced x po_unit_price",
            result=expected_value,
            currency=currency,
            policy_ref=POLICY_CALCULATION,
            note=(
                "Priced on the invoiced quantity so that a partial delivery does not present "
                "as a price variance; quantity is assessed against the receipt separately."
            ),
        )
    )
    calculations.append(
        Calculation(
            name=f"{prefix}_price_variance",
            inputs={
                "invoiced_value": str(line.line_total),
                "expected_value": str(expected_value),
            },
            formula="invoiced_value - expected_value",
            result=variance,
            currency=currency,
            policy_ref=POLICY_CALCULATION,
        )
    )
    calculations.append(
        Calculation(
            name=f"{prefix}_tolerance_limit",
            inputs={
                "line_type": po_line.line_type.value,
                "po_line_value": str(po_line.line_value),
                "permits_freight": str(po_line.permits_freight),
            },
            formula=limit_formula,
            result=limit.amount,
            currency=currency,
            policy_ref=POLICY_TOLERANCE,
        )
    )

    price_within = abs(variance) <= limit.amount
    calculations.append(
        Calculation(
            name=f"{prefix}_within_tolerance",
            inputs={"absolute_variance": str(abs(variance)), "limit": str(limit.amount)},
            formula="abs(variance) <= limit",
            result=abs(variance),
            currency=currency,
            policy_ref=POLICY_TOLERANCE,
            passed=price_within,
        )
    )
    if not price_within:
        exceptions.append(
            ExceptionRecord(
                category=ExceptionCategory.PRICE_VARIANCE,
                failed_rule="three_way_match.line_within_tolerance",
                expected=f"line value of {expected_value} {currency}",
                observed=f"invoiced {line.line_total} {currency}",
                owner=EscalationOwner.REQUESTER,
                policy_refs=[POLICY_TOLERANCE, POLICY_OUTCOMES],
                next_review_date=review,
                detail=(
                    f"Line {line.line_number} ({po_line.line_type.value}): difference "
                    f"{variance} {currency} against the applicable threshold of "
                    f"{limit.amount} {currency}, derived as {limit_formula}."
                ),
            )
        )

    has_receipt, quantity_received = _line_has_receipt_evidence(po, po_line)
    quantity_within = has_receipt and line.quantity <= quantity_received
    calculations.append(
        Calculation(
            name=f"{prefix}_quantity_check",
            inputs={
                "quantity_invoiced": str(line.quantity),
                "quantity_received": str(quantity_received),
            },
            formula="quantity_invoiced <= quantity_received",
            result=quantize(quantity_received - line.quantity),
            currency=None,
            policy_ref=POLICY_TOLERANCE,
            passed=quantity_within,
            note="Quantity units, not currency.",
        )
    )
    if not has_receipt:
        exceptions.append(
            ExceptionRecord(
                category=ExceptionCategory.MISSING_RECEIPT,
                failed_rule="three_way_match.line_receipt_recorded",
                expected=(
                    f"a recorded receipt for purchase-order line {po_line.line_number}"
                    if po_line.line_type is not LineType.SERVICE
                    else (
                        "service completion confirmed for purchase-order line "
                        f"{po_line.line_number}"
                    )
                ),
                observed="no receipt or completion confirmation exists for this line",
                owner=EscalationOwner.RECEIPTER,
                policy_refs=[POLICY_MISSING_RECEIPT],
                next_review_date=review,
                detail=(
                    "Receipt must not be inferred from delivery language on the invoice "
                    "(FIN-POL-002 §4)."
                ),
            )
        )
    elif line.quantity > quantity_received:
        exceptions.append(
            ExceptionRecord(
                category=ExceptionCategory.QUANTITY_VARIANCE,
                failed_rule="three_way_match.quantity_not_above_received",
                expected=(
                    f"invoiced quantity at or below the received quantity of {quantity_received}"
                ),
                observed=f"invoiced quantity {line.quantity}",
                owner=EscalationOwner.RECEIPTER,
                policy_refs=[POLICY_TOLERANCE, POLICY_OUTCOMES],
                next_review_date=review,
                detail=(
                    f"Line {line.line_number}: invoiced {line.quantity} against receipted "
                    f"{quantity_received}, a shortfall of {line.quantity - quantity_received}."
                ),
            )
        )

    return (
        LineMatch(
            invoice_line_number=line.line_number,
            po_line_number=po_line.line_number,
            line_type=po_line.line_type,
            expected_value=expected_value,
            invoiced_value=line.line_total,
            variance=variance,
            tolerance_limit=limit.amount,
            quantity_invoiced=line.quantity,
            quantity_received=quantity_received,
            within_tolerance=price_within and quantity_within,
            matched=True,
        ),
        calculations,
        exceptions,
    )
