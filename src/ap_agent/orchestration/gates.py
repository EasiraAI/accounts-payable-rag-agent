"""Budgets and the approval gate.

Both are separated from the state machine so that the conditions under which this system will
act can be read without reading the machine that acts.

## Budgets

Two counters, both finite, both spent by retries as well as first attempts. A retry that did
not spend budget would make the budget describe an intention rather than a bound: a
dependency failing on every attempt could otherwise consume unbounded wall-clock time inside
a run whose step count looked healthy.

## The approval gate

``authorise_decision`` is the only function in the system that returns permission to call the
write tool, and it is a pure function of stored state. It reads the run and the approval
record and returns a verdict; it takes no argument through which a caller could assert that
approval was obtained.

The write tool re-checks the same conditions independently. Two gates rather than one,
because the property being protected is that money cannot move without a human decision, and
a single enforcement point for that property is a single point of failure.
"""

from __future__ import annotations

from dataclasses import dataclass

from ap_agent.domain.enums import ApprovalStatus, Outcome, RunPhase
from ap_agent.domain.errors import BudgetExhausted
from ap_agent.domain.results import ApprovalRequest
from ap_agent.domain.run_state import RunState


@dataclass(frozen=True)
class Budget:
    """The step and tool-call ceilings for a run."""

    max_steps: int
    max_tool_calls: int

    def check_step(self, state: RunState) -> None:
        """Raise if the run has no step budget left."""
        if state.steps_used >= self.max_steps:
            raise BudgetExhausted("step", self.max_steps)

    def steps_remaining(self, state: RunState) -> int:
        return max(0, self.max_steps - state.steps_used)

    def tool_calls_remaining(self, state: RunState) -> int:
        return max(0, self.max_tool_calls - state.tool_calls_used)


@dataclass(frozen=True)
class DecisionAuthorisation:
    """The verdict on whether a decision may be recorded, and why."""

    permitted: bool
    reason: str
    approval: ApprovalRequest | None = None
    outcome: Outcome | None = None

    @classmethod
    def denied(cls, reason: str) -> DecisionAuthorisation:
        return cls(permitted=False, reason=reason)


def authorise_decision(
    state: RunState,
    approval: ApprovalRequest | None,
) -> DecisionAuthorisation:
    """Decide whether this run may record a consequential outcome.

    Every condition below is a reason to refuse. They are checked in order of how badly
    proceeding would go, and the reason is returned rather than raised so the orchestrator
    can record it as an event and reach a terminal state cleanly.
    """
    if state.recommendation is None:
        return DecisionAuthorisation.denied("the run has no recommendation to act on")

    outcome = state.recommendation.outcome

    if not outcome.is_consequential:
        return DecisionAuthorisation.denied(
            f"{outcome.value} is not a consequential outcome and records no decision"
        )

    if approval is None:
        return DecisionAuthorisation.denied(
            "no approval record exists for this run; FIN-POL-001 §3 makes a recommendation "
            "not an approval"
        )

    if approval.run_id != state.run_id:
        return DecisionAuthorisation.denied(
            f"approval {approval.approval_id} belongs to run {approval.run_id}"
        )

    if approval.status is ApprovalStatus.PENDING:
        return DecisionAuthorisation.denied(
            f"approval {approval.approval_id} is still pending a human decision"
        )

    if approval.status is ApprovalStatus.REJECTED:
        return DecisionAuthorisation.denied(
            f"approval {approval.approval_id} was rejected by {approval.decided_by}"
        )

    if not approval.signature_requirement_met:
        # FIN-POL-003 §3 requires two approvals for a higher-risk transaction, one of them
        # from Financial Control. An earlier version of this gate never read the requirement:
        # it was computed, stored, displayed to the approver, and ignored, so a single
        # signature posted a bank-change case. Two independent reviews found the same gap.
        return DecisionAuthorisation.denied(
            f"approval {approval.approval_id} does not meet its signature requirement: "
            f"{approval.outstanding_requirement_detail()}"
        )

    if approval.requested_outcome is not outcome:
        # The approver saw and decided on one outcome. Using their decision for a different
        # one would spend their authority on something they were never shown.
        return DecisionAuthorisation.denied(
            f"approval {approval.approval_id} authorises "
            f"{approval.requested_outcome.value}, but the recommendation is {outcome.value}"
        )

    if state.decision is not None:
        return DecisionAuthorisation.denied(
            f"run already recorded decision {state.decision.decision_ref}"
        )

    # The decision must be for the transaction the approver was shown. A security review
    # parked a run at the gate, mutated the request amount, and watched the inflated figure
    # post: this gate and the tool both compared only the run and the outcome, while the
    # approval record held the presented amount and nothing read it.
    invoice = state.request.to_invoice()
    if approval.presented_vendor_id:
        presented = (
            approval.presented_amount,
            approval.presented_currency.upper(),
            approval.presented_vendor_id,
        )
        current = (invoice.gross_amount, invoice.currency.upper(), invoice.vendor_id)
        if presented != current:
            return DecisionAuthorisation.denied(
                f"approval {approval.approval_id} authorises "
                f"{approval.presented_amount} {approval.presented_currency} to vendor "
                f"{approval.presented_vendor_id}, but the run now carries "
                f"{invoice.gross_amount} {invoice.currency} to {invoice.vendor_id}; an "
                "approver's authority applies to the figures they were shown"
            )

    return DecisionAuthorisation(
        permitted=True,
        reason=(
            f"approval {approval.approval_id} was granted by {approval.decided_by} "
            f"({approval.decided_by_role}) for {outcome.value}"
        ),
        approval=approval,
        outcome=outcome,
    )


def requires_approval_stop(state: RunState) -> bool:
    """Whether the run must stop and wait for a human at this point."""
    return (
        state.recommendation is not None
        and state.recommendation.requires_approval
        and state.decision is None
    )


#: Phases whose work is safe to repeat after a resume. Every one is a read or a pure
#: computation, so re-running it produces the same state. ``EXECUTE_DECISION`` is absent
#: deliberately: it is the one phase with an external effect, and it is made safe by the
#: idempotency key rather than by being repeatable.
REPEATABLE_PHASES: frozenset[RunPhase] = frozenset(
    {
        RunPhase.INTAKE,
        RunPhase.RETRIEVE_POLICY,
        RunPhase.GATHER_EVIDENCE,
        RunPhase.RECONCILE,
        RunPhase.ASSESS_RISK,
        RunPhase.RECOMMEND,
    }
)
