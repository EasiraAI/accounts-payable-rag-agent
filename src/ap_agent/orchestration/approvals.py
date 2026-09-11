"""Approval semantics: creating the request, and recognising a delivery that changes nothing.

Separated from the orchestrator because these are the two places where the system decides what
an approval *means*, and they are what a finance-controls reviewer opens first. The driver that
moves a run between phases is a different concern and stays in ``machine.py``.

``create_approval`` computes the FIN-POL-003 §3 signature requirement and binds the amount,
currency and vendor that were presented, so the case cannot mutate between recommendation and
gate.

``short_circuit_replay`` answers the question an at-least-once delivery forces: is this the
same person signing again? It runs before anything is spent, because a retry that re-read the
authority register and re-validated authority would contradict the claim, made in three places,
that a duplicate delivery performs no validation and no tool call.

What is *not* here is ``_resolve``. It calls the driver, the emitter and the finaliser, so
moving it would mean handing this module the orchestrator itself, which is a rename rather than
a decomposition.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from ap_agent.domain.enums import ApprovalStatus, EventType, RunPhase
from ap_agent.domain.errors import ApprovalStateConflict
from ap_agent.domain.request import ApprovalDecision
from ap_agent.domain.results import ApprovalRequest, Recommendation
from ap_agent.domain.run_state import RunState, new_approval_id
from ap_agent.observability.events import EventEmitter
from ap_agent.persistence.repository import Repository


def create_approval(
    repository: Repository,
    clock: Callable[[], datetime],
    state: RunState,
    emitter: EventEmitter,
    recommendation: Recommendation,
) -> None:
    """Create the pending approval and stop.

    The record captures what the approver will be shown, not only what they decide.
    FIN-POL-003 §5 requires the approver to see the amount, vendor, exceptions and
    citations before deciding, and storing what was presented is how that requirement
    becomes auditable after the fact.
    """
    if state.approval_id is not None:
        existing = repository.load_approval(state.approval_id)
        if existing is not None:
            return

    invoice = state.request.to_invoice()
    request = ApprovalRequest(
        approval_id=new_approval_id(),
        run_id=state.run_id,
        case_id=state.case_id,
        requested_outcome=recommendation.outcome,
        presented_amount=invoice.gross_amount,
        presented_currency=invoice.currency,
        presented_vendor=invoice.vendor_name,
        presented_vendor_id=invoice.vendor_id,
        presented_exception_categories=[
            exception.category for exception in recommendation.exceptions
        ],
        presented_citations=recommendation.cited_evidence[:12],
        required_role_minimum=state.required_role_minimum,
        higher_risk_reasons=list(state.higher_risk_reasons),
        # FIN-POL-003 §3: two approvals for a higher-risk transaction, one of them from
        # Financial Control. Recorded as a requirement the gate enforces, not as a flag
        # nobody reads.
        required_signature_count=2 if recommendation.requires_second_approval else 1,
        requires_financial_control=recommendation.requires_second_approval,
        created_at=clock(),
    )
    repository.create_approval(request)
    state.approval_id = request.approval_id
    emitter.emit(
        EventType.APPROVAL_REQUESTED,
        payload={
            "approval_id": request.approval_id,
            "requested_outcome": request.requested_outcome.value,
            "amount": str(request.presented_amount),
            "currency": request.presented_currency,
            "requires_second_approval": request.requires_second_approval,
            "presented_exception_categories": [
                category.value for category in request.presented_exception_categories
            ],
            "presented_citation_count": len(request.presented_citations),
        },
        phase=RunPhase.RECOMMEND,
        outcome="AWAITING_HUMAN_DECISION",
    )


def short_circuit_replay(
    repository: Repository,
    state: RunState,
    emitter: EventEmitter,
    approval: ApprovalRequest,
    decision: ApprovalDecision,
    status: ApprovalStatus,
) -> tuple[RunState, bool] | None:
    """Handle a delivery that changes nothing, before anything is spent on it.

    Returns the answer when the delivery is a replay, and raises when it contradicts a
    settled decision. Returns ``None`` when the delivery has work to do and the caller
    should proceed.

    One approver approving and another rejecting the same request is a real disagreement,
    so it raises rather than being resolved by arrival order.

    The first test is the approver's own signature, and it comes before the status tests
    deliberately. An earlier version asked only whether the approval was settled, so a
    repeated delivery against a *pending* two-signature approval — the commonest retry
    there is, since the caller has not seen the gate open — spent a tool call re-reading
    the authority register and re-validated authority before the signature table's primary
    key detected the duplicate. Nothing was recorded twice, but the claim made in three
    places that a duplicate delivery performs no validation and no tool call was untrue
    for exactly the case where retries are most likely.
    """
    if status is ApprovalStatus.APPROVED and any(
        signature.approver_id == decision.approver_id for signature in approval.signatures
    ):
        emitter.emit(
            EventType.APPROVAL_REPLAYED,
            payload={
                "approval_id": approval.approval_id,
                "status": approval.status.value,
                "approver_id": decision.approver_id,
                "signatures_collected": approval.signatures_collected,
                "note": (
                    "this approver has already signed; no validation, no tool call, no state change"
                ),
            },
            phase=state.phase,
            outcome="REPLAYED",
        )
        return repository.require_run(state.run_id), True
    if approval.status is ApprovalStatus.REJECTED:
        if status is ApprovalStatus.REJECTED:
            emitter.emit(
                EventType.APPROVAL_REPLAYED,
                payload={
                    "approval_id": approval.approval_id,
                    "status": approval.status.value,
                    "originally_decided_by": approval.decided_by,
                    "note": "duplicate rejection; no validation or state change",
                },
                phase=state.phase,
                outcome="REPLAYED",
            )
            return repository.require_run(state.run_id), True
        raise ApprovalStateConflict(
            f"approval {approval.approval_id} is already REJECTED and cannot be changed to APPROVED"
        )

    if approval.status is ApprovalStatus.APPROVED:
        if status is ApprovalStatus.REJECTED:
            raise ApprovalStateConflict(
                f"approval {approval.approval_id} is already APPROVED and cannot be "
                "changed to REJECTED"
            )
        # A repeat from an approver who already signed was answered above, whatever the
        # status. Reaching here means a *different* person is approving something already
        # settled, which is not a replay: the requirement was met without them.
        raise ApprovalStateConflict(
            f"approval {approval.approval_id} is already APPROVED; its signature "
            "requirement was met and the decision has been settled"
        )

    return None
