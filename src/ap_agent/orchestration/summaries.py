"""Turning run state into the text a person reads.

Pulled out of the orchestrator, where they were seven static methods among thirty others. An
audit observed that ADR-0002's case for going framework-free rests on a reviewer being able to
read the orchestrator end to end, and that the file had grown to 2,154 lines: the one module a
finance-controls reviewer would actually open was the least readable in the repository.

Every function here is pure. Given a run state they return text, touch no store, call no model
and read no clock, which is what makes them testable directly and what makes it safe for the
deterministic summary to be the fallback when the model's own prose is discarded.

Two of them carry a control rather than a convenience:

``computed_values`` is the set the narrative screen checks figures against. A number in
approver-facing prose that does not appear here was invented by the model, so what this
function omits is not a formatting detail; it is a false positive waiting to happen, which is
why it reaches into identifiers as well as amounts.

``with_payment_schedule`` appends the engine's own payment-run date to the next action. The
model writes what a person should do; the date comes from FIN-POL-006 §2 arithmetic, because a
date produced by a model would be exactly the free-form arithmetic FIN-POL-002 §5 forbids.
"""

from __future__ import annotations

from ap_agent.domain.enums import Outcome
from ap_agent.domain.results import truncate_detail
from ap_agent.domain.run_state import RunState


def computed_values(state: RunState) -> list[str]:
    """Every figure the engine computed, as text, for the grounding check.

    Assembled from the calculation records, the exception records and the run's own
    amounts rather than from a curated list, so a control added later contributes its
    figures without anyone remembering to extend this. A figure absent from here and
    present in the prose is one the model invented.
    """
    values: list[str] = []
    request = state.request
    values.extend([str(request.amount), request.currency, request.invoice_reference])
    if request.net_amount is not None:
        values.append(str(request.net_amount))
    if request.tax_amount is not None:
        values.append(str(request.tax_amount))
    if request.invoice_date is not None:
        values.append(request.invoice_date.isoformat())
    if request.po_reference:
        values.append(request.po_reference)
    if state.payable_on is not None:
        values.append(state.payable_on.isoformat())
    if state.proposed_payment_run is not None:
        values.append(state.proposed_payment_run.isoformat())
    for calculation in state.calculations:
        values.append(str(calculation.result))
        values.extend(str(value) for value in calculation.inputs.values())
    for exception in state.exceptions:
        values.extend([exception.expected, exception.observed, exception.detail])
    for record in state.invoice_history:
        values.extend([record.record_id, record.invoice_reference, str(record.gross_amount)])
    for line in request.lines:
        values.extend([str(line.quantity), str(line.unit_price), str(line.line_total)])
    # Identifiers, because they contain digit runs and the figure check cannot tell an
    # approver reference from a fabricated amount. Naming the approver is exactly what a
    # correct summary does, so leaving these out made the check flag good prose: a unit
    # test on "the invoice has been approved by U-3081" caught it.
    values.extend(
        value
        for value in (
            request.vendor_id,
            request.requested_by,
            request.cost_centre,
            state.run_id,
            state.approval_id,
        )
        if value
    )
    if state.delegation is not None:
        values.extend([state.delegation.delegation_id, state.delegation.delegate_id])
    if state.vendor is not None:
        values.append(state.vendor.vendor_id)
    if state.purchase_order is not None:
        values.append(state.purchase_order.po_reference)
    return values


def deterministic_summary(state: RunState, outcome: Outcome) -> str:
    """A summary built from computed facts, with no model involvement."""
    categories = sorted({exception.category.value for exception in state.exceptions})
    indicators = [indicator.code for indicator in state.fraud_indicators]
    parts = [f"The rule engine computed {outcome.value} from the control results."]
    if categories:
        parts.append("Exceptions raised: " + ", ".join(categories) + ".")
    else:
        parts.append("No exceptions were raised.")
    if indicators:
        parts.append("Fraud indicators: " + ", ".join(indicators) + ".")
    if state.unknowns:
        parts.append(f"{len(state.unknowns)} item(s) remain unknown.")
    parts.append(
        "This summary was generated from the computed results because the model's prose "
        "was screened out."
    )
    return " ".join(parts)[:1_190]


def with_payment_schedule(state: RunState, next_action: str, outcome: Outcome) -> str:
    """Append the engine-computed payment schedule to the next action.

    The model writes the prose and never the date. FIN-POL-006 §2 schedules an approved
    invoice for a standard run before its due date, and that run is computed by
    ``rules/payment_terms.py`` from the agreed terms; a date produced by a model would be
    arithmetic taken from the model, which is what FIN-POL-002 §5 and this system's whole
    division of labour forbid.

    Appended rather than substituted because the model's sentence says what a person
    should *do* and this says when the payment would land. Only for an approval: naming a
    run date on a rejected or held case would describe a payment that is not going to
    happen.
    """
    if outcome is not Outcome.APPROVE_FOR_POSTING or state.payable_on is None:
        return next_action
    if state.proposed_payment_run is None:
        schedule = (
            f" The invoice is payable {state.payable_on.isoformat()} and no standard "
            "payment run falls before that date, so scheduling is for Accounts Payable to "
            "resolve; internal delay alone is not grounds for a manual payment "
            "(FIN-POL-006 §3)."
        )
    else:
        schedule = (
            f" Proposed payment run {state.proposed_payment_run.isoformat()}, ahead of the "
            f"due date {state.payable_on.isoformat()} (FIN-POL-006 §2). A proposal only: "
            "an agent may prepare a schedule and may not release a payment file."
        )
    return truncate_detail(next_action.rstrip() + schedule)


def deterministic_next_action(state: RunState, outcome: Outcome) -> str:
    if outcome is Outcome.APPROVE_FOR_POSTING:
        # The proposed run is named when one exists. FIN-POL-006 §2 schedules an approved
        # invoice for the next standard run before its due date, and a next action that
        # says "the next standard run" without saying which one leaves the reader to
        # recompute a date the engine has already computed.
        if state.proposed_payment_run is not None:
            schedule = (
                f"then post and schedule for the standard payment run on "
                f"{state.proposed_payment_run.isoformat()}, ahead of the due date "
                f"{state.payable_on.isoformat() if state.payable_on else 'not computed'} "
                "(FIN-POL-006 §2). Proposal only: an agent may not release a payment file."
            )
        else:
            schedule = (
                "then post. No standard payment run remains before the due date, so "
                "scheduling is for Accounts Payable to resolve; internal delay alone is "
                "not grounds for a manual payment (FIN-POL-006 §3)."
            )
        return "Route to an approver holding at least the required delegated authority, " + schedule
    if outcome is Outcome.ESCALATE_CONTROL_REVIEW:
        return (
            "Refer to Financial Crime and Controls without notifying the supplier of "
            "the suspicion (FIN-POL-007 §3)."
        )
    if outcome in {Outcome.REJECT_DUPLICATE, Outcome.REJECT_INVALID}:
        return (
            "Route the rejection to an approver, then notify the requester with the cited records."
        )
    return (
        "Route each exception to the owner named in its record and resume from the "
        "failed control when new evidence arrives."
    )


def vendor_summary(state: RunState) -> str:
    vendor = state.vendor
    if vendor is None:
        return "  (no vendor master record was returned)"
    return "\n".join(
        [
            f"  vendor_id: {vendor.vendor_id}",
            f"  legal_name: {vendor.legal_name}",
            f"  status: {vendor.status.value}",
            f"  bank_account_last4: {vendor.bank_account_last4 or '(none on file)'}",
            f"  bank_country: {vendor.bank_country or '(unknown)'}",
            f"  bank_details_changed_at: {vendor.bank_details_changed_at or '(never)'}",
            f"  created_at: {vendor.created_at}",
            f"  risk_flags: {vendor.risk_flags or '(none)'}",
            f"  on_payment_hold: {vendor.on_payment_hold}",
        ]
    )


def order_summary(state: RunState) -> str:
    order = state.purchase_order
    if order is None:
        return "  (no purchase order was available)"
    lines = [
        f"  po_reference: {order.po_reference}",
        f"  currency: {order.currency}",
        f"  total_value: {order.total_value}",
        f"  approval_status: {order.approval_status}",
        f"  line_count: {len(order.lines)}",
        f"  receipt_count: {len(order.receipts)}",
    ]
    for line in order.lines:
        lines.append(
            f"    line {line.line_number} ({line.line_type.value}): "
            f"ordered {line.quantity_ordered} at {line.unit_price} "
            f"= {line.line_value}, received "
            f"{order.quantity_received_for(line.line_number)}"
        )
    return "\n".join(lines)


def history_summary(state: RunState) -> str:
    if not state.invoice_history:
        return "  (no prior records on file for this vendor)"
    return "\n".join(
        f"  {record.record_id}: {record.invoice_reference}, {record.gross_amount} "
        f"{record.currency}, {record.invoice_date}, status {record.status.value}"
        for record in state.invoice_history
    )
