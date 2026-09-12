"""Apply every deterministic control. No model, no tools, no I/O.

Extracted from the orchestrator so that what this phase can reach is visible in its
signature: a clock, a repository, a retriever and a model call, and nothing else.
"""

from __future__ import annotations

from ap_agent.domain.enums import (
    EventType,
    RunPhase,
)
from ap_agent.domain.money import Money
from ap_agent.domain.results import (
    Unknown,
)
from ap_agent.domain.rules.authority import required_authority
from ap_agent.domain.rules.duplicates import duplicate_check
from ap_agent.domain.rules.matching import three_way_match
from ap_agent.domain.rules.non_po import check_repeated_non_po
from ap_agent.domain.rules.payment_instructions import check_payment_instructions
from ap_agent.domain.rules.payment_terms import assess_payment_terms
from ap_agent.domain.rules.segregation import (
    check_reconciliation_time as check_segregation_at_reconciliation,
)
from ap_agent.domain.rules.tax import assess_tax
from ap_agent.domain.rules.vendor import vendor_status_check
from ap_agent.domain.run_state import RunState
from ap_agent.observability.events import EventEmitter
from ap_agent.orchestration.phase_context import PhaseContext
from ap_agent.tools.base import ToolRunner
from ap_agent.tools.mock_backends import MockBackends


def run(
    ctx: PhaseContext,
    state: RunState,
    emitter: EventEmitter,
    runner: ToolRunner,
    backends: MockBackends,
) -> None:
    """Apply the deterministic controls. No model, no tools, no I/O."""
    as_of = ctx.clock()
    invoice = state.request.to_invoice()

    match = three_way_match(invoice, state.purchase_order, as_of=as_of)
    duplicates = duplicate_check(invoice, state.invoice_history, as_of=as_of)
    vendor = vendor_status_check(
        state.vendor,
        invoice,
        as_of=as_of,
        requested_by=state.request.requested_by,
        history=state.invoice_history,
    )
    # FIN-POL-001 §5 names this as a required check and an earlier version had no such
    # control: the only thing standing between an asserted new account and a payment was
    # a text heuristic looking for urgency.
    instructions = check_payment_instructions(
        vendor=state.vendor, texts=state.request.untrusted_texts(), as_of=as_of
    )
    # FIN-POL-002 §2 requires tax to be assessed separately and forbids it being hidden
    # inside a price variance. The matching engine excludes tax from the comparison; this
    # is the assessment that was missing, and the only path that can raise TAX_QUERY.
    tax = assess_tax(
        invoice,
        tax_separated=state.request.tax_separated,
        purchase_order=state.purchase_order,
        as_of=as_of,
    )
    # FIN-POL-006 §1 to §3. The request has always carried payment terms and nothing read
    # them; §1 makes the printed figure a supplier claim to be compared with the order
    # rather than a value to be used.
    terms = assess_payment_terms(
        invoice,
        purchase_order=state.purchase_order,
        printed_terms_days=state.request.payment_terms_days,
        invoice_date_supplied=state.request.invoice_date_supplied,
        as_of=as_of,
    )
    state.payable_on = terms.payable_on
    state.proposed_payment_run = terms.proposed_run_date
    state.due_date_on_non_business_day = terms.due_date_on_non_business_day
    # FIN-POL-012 §4, the one control in the corpus about a pattern rather than a
    # transaction: repeated non-PO invoicing by one supplier. The history the duplicate
    # check already retrieved carries everything it needs.
    non_po = check_repeated_non_po(invoice, state.invoice_history, as_of=as_of)
    # Only the part of FIN-POL-001 §4 that does not need an approver. The rest is
    # evaluated when a callback arrives; see domain/rules/segregation.py.
    segregation = check_segregation_at_reconciliation(
        vendor=state.vendor,
        purchase_order=state.purchase_order,
        invoice=invoice,
        requested_by=state.request.requested_by,
    )

    for label, result in (
        ("three_way_match", match),
        ("duplicate_check", duplicates),
        ("vendor_status_check", vendor),
        ("payment_instructions", instructions),
        ("segregation_of_duties", segregation),
        ("tax_assessment", tax),
        ("payment_terms", terms),
        ("repeated_non_po", non_po),
    ):
        state.add_exceptions(list(result.exceptions))
        state.add_findings(list(result.findings))
        state.add_calculations(list(getattr(result, "calculations", [])))
        state.add_unknowns(
            _without_superseded_unknowns(state, list(getattr(result, "unknowns", [])))
        )
        state.add_assumptions(list(getattr(result, "assumptions", [])))
        emitter.emit(
            EventType.RULE_EVALUATED,
            payload={
                "rule_group": label,
                "exception_count": len(result.exceptions),
                "finding_count": len(result.findings),
            },
            phase=RunPhase.RECONCILE,
            outcome="SUCCESS",
        )

    authority = required_authority(
        Money(amount=invoice.gross_amount, currency=invoice.currency),
        higher_risk_reasons=vendor.higher_risk_reasons,
    )
    state.add_calculations(list(authority.calculations))
    state.add_findings(list(authority.findings))
    state.add_assumptions(list(authority.assumptions))
    state.required_role_minimum = authority.required_role_minimum.value
    # Every FIN-POL-003 §3 higher-risk condition found anywhere, not only in the vendor
    # record. A bank change asserted in untrusted text is a higher-risk condition too,
    # and it is the approval requirement that has to reflect it.
    risk_reasons = list(vendor.higher_risk_reasons)
    if instructions.exceptions:
        risk_reasons.append(
            "payment instructions in supplied text do not match the verified vendor "
            "master (FIN-POL-001 §5)"
        )
    state.higher_risk_reasons = risk_reasons

    for exception in state.exceptions:
        emitter.emit(
            EventType.EXCEPTION_RAISED,
            payload={
                "category": exception.category.value,
                "failed_rule": exception.failed_rule,
                "owner": exception.owner.value,
                "blocking": exception.blocking,
                "policy_refs": exception.policy_refs,
            },
            phase=RunPhase.RECONCILE,
            outcome="RAISED",
        )


def _without_superseded_unknowns(state: RunState, candidates: list[Unknown]) -> list[Unknown]:
    """Drop a rule's generic unknown when a tool-level one already explains the gap.

    ``three_way_match`` is a pure function: given no purchase order it can only report
    that the order was unavailable. The evidence phase knows more, because it made the
    call and saw a timeout, so it records that the order was not shown to be absent, only
    unreachable. Both statements are true, but presenting both to a reviewer invites the
    reading that two different things went wrong. The more specific record wins.
    """
    specific_sources = {
        unknown.source_attempted for unknown in state.unknowns if unknown.source_attempted
    }
    if "get_purchase_order" not in specific_sources:
        return candidates
    return [unknown for unknown in candidates if "purchase-order lines" not in unknown.item.lower()]
