"""Record the decision, exactly once, after the gate has opened.

Extracted from the orchestrator so that what this phase can reach is visible in its
signature: a clock, a repository, a retriever and a model call, and nothing else.
"""

from __future__ import annotations

import time

from ap_agent.domain.enums import (
    EventType,
    ExceptionCategory,
    RunPhase,
)
from ap_agent.domain.errors import (
    ToolPermissionDenied,
)
from ap_agent.domain.results import (
    ActionRecord,
)
from ap_agent.domain.rules.vendor import vendor_status_check
from ap_agent.domain.run_state import RunState
from ap_agent.observability.events import EventEmitter
from ap_agent.orchestration.gates import authorise_decision
from ap_agent.orchestration.phase_context import PhaseContext
from ap_agent.tools.base import ToolRunner
from ap_agent.tools.contracts import (
    SUBMIT_FINANCE_DECISION,
    SubmitFinanceDecisionInput,
    submit_finance_decision,
)
from ap_agent.tools.mock_backends import MockBackends


def run(
    ctx: PhaseContext,
    state: RunState,
    emitter: EventEmitter,
    runner: ToolRunner,
    backends: MockBackends,
) -> None:
    """Record the approved decision, exactly once."""
    approval = ctx.repository.load_approval(state.approval_id) if state.approval_id else None
    authorisation = authorise_decision(state, approval)
    if not authorisation.permitted or authorisation.approval is None:
        raise ToolPermissionDenied(SUBMIT_FINANCE_DECISION.name, authorisation.reason)

    invoice = state.request.to_invoice()

    # FIN-POL-007 §5 requires a resumed case to "revalidate any time-sensitive vendor or
    # delegation information". The delegation is revalidated when the signature is taken;
    # the vendor was not, and a controls review pointed out that a vendor which became
    # BLOCKED or acquired a risk flag between recommendation and approval would be posted
    # on stale facts. The monetary authority still comes from the stored approval, which
    # is correct: an approver's limit applies to what they were shown. Vendor *status* is
    # a fact about the world, and it is re-read here.
    revalidation = vendor_status_check(
        state.vendor,
        invoice,
        as_of=ctx.clock(),
        requested_by=state.request.requested_by,
        history=state.invoice_history,
    )
    newly_blocking = [
        exception
        for exception in revalidation.exceptions
        if exception.blocking and exception.category is not ExceptionCategory.BANK_CHANGE
    ]
    if newly_blocking:
        state.add_exceptions(newly_blocking)
        emitter.emit(
            EventType.EXCEPTION_RAISED,
            payload={
                "rule_group": "vendor_revalidation_at_decision",
                "categories": sorted({e.category.value for e in newly_blocking}),
                "note": (
                    "vendor facts changed between recommendation and approval; "
                    "FIN-POL-007 §5 requires revalidation before resuming"
                ),
            },
            phase=RunPhase.EXECUTE_DECISION,
            outcome="BLOCKED",
        )
        raise ToolPermissionDenied(
            SUBMIT_FINANCE_DECISION.name,
            "vendor revalidation at decision time found a blocking condition that was "
            "not present at assessment: "
            + "; ".join(exception.observed for exception in newly_blocking),
        )

    arguments = SubmitFinanceDecisionInput(
        run_id=state.run_id,
        case_id=state.case_id,
        approval_id=authorisation.approval.approval_id,
        outcome=authorisation.approval.requested_outcome,
        amount=invoice.gross_amount,
        currency=invoice.currency,
        vendor_id=invoice.vendor_id,
        invoice_reference=invoice.invoice_reference,
    )
    handler = submit_finance_decision(ctx.repository)

    existing = ctx.repository.load_decision(state.run_id)
    started = time.perf_counter()
    receipt = handler(arguments)  # type: ignore[operator]
    duration_ms = int((time.perf_counter() - started) * 1000)

    replayed = receipt.replayed or existing is not None
    state.decision = receipt
    state.actions_taken.append(
        ActionRecord(
            action=f"RECORD_{receipt.outcome.value}",
            target=receipt.posting_system,
            performed_at=receipt.recorded_at,
            reference=receipt.decision_ref,
            idempotency_key=receipt.idempotency_key,
            simulated=True,
            detail=(
                f"Authorised by {authorisation.approval.decided_by} "
                f"({authorisation.approval.decided_by_role}); "
                f"{authorisation.reason}"
            ),
        )
    )
    emitter.emit(
        EventType.DECISION_REPLAYED if replayed else EventType.DECISION_SUBMITTED,
        payload={
            "decision_ref": receipt.decision_ref,
            "outcome": receipt.outcome.value,
            "amount": str(receipt.amount),
            "currency": receipt.currency,
            "posting_system": receipt.posting_system,
            "simulated": True,
            "replayed": replayed,
        },
        phase=RunPhase.EXECUTE_DECISION,
        outcome="REPLAYED" if replayed else "RECORDED",
        duration_ms=duration_ms,
    )
