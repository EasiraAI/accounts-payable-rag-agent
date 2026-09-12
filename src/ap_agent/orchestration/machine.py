"""The state machine that drives a run.

Framework-free by decision, argued in docs/adr/0002-orchestration-framework.md. Every
transition, budget check and gate is in this file, in execution order, so the control flow
can be read end to end by someone who needs to satisfy themselves about what the system will
and will not do.

## The shape of a run

    INTAKE -> RETRIEVE_POLICY -> GATHER_EVIDENCE -> RECONCILE -> ASSESS_RISK -> RECOMMEND
                                                                                    |
                                          +-----------------------------------------+
                                          |                                         |
                              requires approval                          no approval needed
                                          |                                         |
                                AWAITING_APPROVAL  --approve-->  EXECUTE_DECISION   |
                                          |                             |           |
                                       reject                           v           v
                                          |                        COMPLETED    HELD / COMPLETED
                                          v
                                        HELD

``AWAITING_APPROVAL`` is a pause, not an end. The driver returns, the process is free to
exit, and the run resumes when a human decides.

## Where the model is, and is not

Two phases call the model, and neither decides anything. ``ASSESS_RISK`` asks it to read
retrieved text into structured statements; ``RECOMMEND`` asks it to write the explanation an
approver reads. The outcome is computed by the rule engine from typed facts before the model
is asked for narrative, and a model suggestion is applied only if it makes the outcome more
conservative.

## Why every phase is safe to re-run

A resumed run re-enters at its stored phase. Every phase in the linear plan performs reads
and pure computation, and the state accumulators suppress duplicates, so re-running one
converges rather than double-counting. ``EXECUTE_DECISION`` is the single phase with an
external effect, and it is made safe by the idempotency key rather than by being repeatable.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from ap_agent.config.settings import Settings
from ap_agent.domain.enums import (
    ApprovalStatus,
    EventType,
    FailureReason,
    RunPhase,
    RunStatus,
)
from ap_agent.domain.errors import (
    ApprovalStateConflict,
    BudgetExhausted,
    ModelError,
    ModelOutputInvalid,
    ToolPermissionDenied,
)
from ap_agent.domain.evidence import DelegationRecord
from ap_agent.domain.request import ApprovalDecision, ProcessingRequest
from ap_agent.domain.results import (
    ActionRecord,
    ApprovalSignature,
    FinalResult,
    Unknown,
)
from ap_agent.domain.run_state import RunState, utc_now
from ap_agent.llm import (
    SYSTEM_PROMPT,
    LLMClient,
)
from ap_agent.observability.events import EventEmitter
from ap_agent.orchestration.approvals import authorise_signature, short_circuit_replay
from ap_agent.orchestration.gates import Budget
from ap_agent.orchestration.phases import (
    assess_risk,
    execute_decision,
    gather_evidence,
    intake,
    next_phase,
    recommend,
    reconcile,
    retrieve_policy,
)
from ap_agent.persistence.repository import Repository
from ap_agent.rag.retriever import Retriever
from ap_agent.tools.base import ToolRunner
from ap_agent.tools.contracts import (
    GET_AUTHORITY_DELEGATION,
    GetDelegationInput,
    GetDelegationOutput,
    get_authority_delegation,
)
from ap_agent.tools.mock_backends import MockBackends

Clock = Callable[[], datetime]


class Orchestrator:
    """Drives runs through the phase plan, stopping at the approval gate."""

    def __init__(
        self,
        *,
        settings: Settings,
        repository: Repository,
        retriever: Retriever,
        llm_client: LLMClient,
        clock: Clock | None = None,
        mock_data_dir: Any = None,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._retriever = retriever
        self._llm = llm_client
        self._clock = clock or utc_now
        self._mock_data_dir = mock_data_dir
        self._budget = Budget(max_steps=settings.max_steps, max_tool_calls=settings.max_tool_calls)

    # ---- public entry points ----------------------------------------------------------

    def start(self, request: ProcessingRequest) -> RunState:
        """Create a run and execute it until completion, failure, or the approval gate."""
        state = RunState.create(request, now=self._clock())
        state = self._repository.create_run(state)
        emitter = self._emitter(state)
        emitter.emit(
            EventType.RUN_CREATED,
            payload={
                "case_id": state.case_id,
                "invoice_reference": request.invoice_reference,
                "vendor": request.vendor,
                "amount": str(request.amount),
                "currency": request.currency,
                "attachment_count": len(request.attachments),
                "has_notes": request.notes is not None,
                "max_steps": self._budget.max_steps,
                "max_tool_calls": self._budget.max_tool_calls,
                "provider": self._llm.provider_name,
                "model": self._llm.model_name,
            },
            phase=state.phase,
        )
        return self._drive(state, emitter)

    def resume(self, run_id: str) -> RunState:
        """Re-enter a run at its stored phase.

        The same code path a fresh process takes after a restart. There is no separate
        recovery routine, which is what makes the resume behaviour testable: the test simply
        constructs a new repository connection and calls this.
        """
        state = self._repository.require_run(run_id)
        emitter = self._emitter(state)
        if state.is_terminal:
            return state
        emitter.emit(
            EventType.RUN_RESUMED,
            payload={"phase": state.phase.value, "steps_used": state.steps_used},
            phase=state.phase,
        )
        return self._drive(state, emitter)

    def approve(self, run_id: str, decision: ApprovalDecision) -> tuple[RunState, bool]:
        """Resolve a pending approval as approved and continue the run.

        Returns the run and whether the callback was a replay. A replayed callback returns
        the same state and records an ``APPROVAL_REPLAYED`` event; it never re-executes the
        decision.
        """
        return self._resolve(run_id, decision, ApprovalStatus.APPROVED)

    def reject(self, run_id: str, decision: ApprovalDecision) -> tuple[RunState, bool]:
        """Resolve a pending approval as rejected.

        No decision is recorded: a rejection of a posting recommendation means nothing is
        posted. The case is held so accounts payable retains it, rather than completed, which
        would imply it had been dealt with.
        """
        return self._resolve(run_id, decision, ApprovalStatus.REJECTED)

    # ---- driver -----------------------------------------------------------------------

    # ---- the phase context -------------------------------------------------------------
    #
    # Four public members, which is exactly what orchestration/phase_context.py requires. The
    # orchestrator satisfies that protocol structurally, so a phase receives ``self`` and can
    # still only reach these. Keeping them read-only properties over the private attributes
    # means the narrowing is real rather than a naming convention.

    @property
    def clock(self) -> Callable[[], datetime]:
        return self._clock

    @property
    def repository(self) -> Repository:
        return self._repository

    @property
    def retriever(self) -> Retriever:
        return self._retriever

    def _emitter(self, state: RunState) -> EventEmitter:
        return EventEmitter(
            repository=self._repository,
            run_id=state.run_id,
            correlation_id=state.correlation_id,
        )

    def _backends(self, state: RunState) -> MockBackends:
        return MockBackends(
            data_dir=self._mock_data_dir,
            case_id=state.case_id,
            as_of=self._clock(),
        )

    def _drive(self, state: RunState, emitter: EventEmitter) -> RunState:
        """Advance the run until it is terminal or waiting for a human.

        The loop is bounded twice: by the step budget, and by the phase plan being a finite
        linear sequence. There is no condition under which it can run indefinitely.
        """
        backends = self._backends(state)
        runner = ToolRunner(
            emitter=emitter,
            max_tool_calls=self._budget.max_tool_calls,
            # A resumed run has already spent budget; the runner continues from there.
            calls_already_used=state.tool_calls_used,
        )

        while True:
            if state.phase.is_terminal or state.phase is RunPhase.AWAITING_APPROVAL:
                return state
            try:
                self._budget.check_step(state)
            except BudgetExhausted as error:
                emitter.emit(
                    EventType.BUDGET_EXCEEDED,
                    payload={"kind": error.kind, "limit": error.limit},
                    phase=state.phase,
                    outcome="FAILED",
                )
                return self._fail(state, emitter, FailureReason.BUDGET_EXHAUSTED, str(error))

            try:
                state = self._execute_phase(state, emitter, runner, backends)
            except ModelOutputInvalid as error:
                return self._fail(
                    state, emitter, FailureReason.MODEL_OUTPUT_INVALID, error.validation_error
                )
            except ModelError as error:
                return self._fail(state, emitter, FailureReason.INTERNAL_ERROR, str(error))
            except ToolPermissionDenied as error:
                return self._fail(
                    state, emitter, FailureReason.APPROVAL_STATE_CONFLICT, error.message
                )

    def _execute_phase(
        self,
        state: RunState,
        emitter: EventEmitter,
        runner: ToolRunner,
        backends: MockBackends,
    ) -> RunState:
        """Run one phase, persist the result, and move to the next."""
        phase = state.phase
        emitter.emit(EventType.PHASE_STARTED, phase=phase)
        started = time.perf_counter()

        handler = {
            RunPhase.INTAKE: intake.run,
            RunPhase.RETRIEVE_POLICY: retrieve_policy.run,
            RunPhase.GATHER_EVIDENCE: gather_evidence.run,
            RunPhase.RECONCILE: reconcile.run,
            RunPhase.ASSESS_RISK: assess_risk.run,
            RunPhase.RECOMMEND: recommend.run,
            RunPhase.EXECUTE_DECISION: execute_decision.run,
        }[phase]

        # ``self`` is the context. The protocol in phase_context.py is four members wide, so
        # what a phase can reach is a stated contract rather than whatever the orchestrator
        # happens to expose.
        handler(self, state, emitter, runner, backends)

        state.steps_used += 1
        state.tool_calls_used = runner.calls_used
        state.mark_phase_complete(phase)
        state.phase = self._transition_from(phase, state)
        state.status = self._status_for(state.phase)
        state = self._repository.save_run(state)

        emitter.emit(
            EventType.PHASE_COMPLETED,
            payload={
                "next_phase": state.phase.value,
                "steps_used": state.steps_used,
                "tool_calls_used": state.tool_calls_used,
            },
            phase=phase,
            outcome="SUCCESS",
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
        if state.phase.is_terminal:
            self._finalise(state, emitter)
        return state

    def _transition_from(self, phase: RunPhase, state: RunState) -> RunPhase:
        """Where the run goes after a phase completes."""
        if phase is RunPhase.EXECUTE_DECISION:
            return RunPhase.COMPLETED
        if phase is RunPhase.RECOMMEND:
            recommendation = state.recommendation
            if recommendation is None:
                return RunPhase.FAILED
            if recommendation.requires_approval:
                return RunPhase.AWAITING_APPROVAL
            # A hold or an escalation needs no approver: the case moves inside accounts
            # payable and is complete as far as this run is concerned.
            return RunPhase.HELD
        following = next_phase(phase)
        return following if following is not None else RunPhase.COMPLETED

    @staticmethod
    def _status_for(phase: RunPhase) -> RunStatus:
        return {
            RunPhase.AWAITING_APPROVAL: RunStatus.AWAITING_APPROVAL,
            RunPhase.COMPLETED: RunStatus.COMPLETED,
            RunPhase.HELD: RunStatus.HELD,
            RunPhase.FAILED: RunStatus.FAILED,
        }.get(phase, RunStatus.RUNNING)

    # ---- phases -----------------------------------------------------------------------

    # Nothing is cached on the orchestrator. The rule functions are pure, so RECOMMEND
    # recomputes them from the same persisted evidence and gets the same answer.
    # Caching on the instance would outlive a run and would not survive a resume, so it
    # would be both unsafe under concurrency and useless where it was needed.

    # ---- approval handling -------------------------------------------------------------

    def _resolve(
        self, run_id: str, decision: ApprovalDecision, status: ApprovalStatus
    ) -> tuple[RunState, bool]:
        """Apply a human decision to a run, idempotently.

        An approval is a set of signatures, not a single decision. FIN-POL-003 §3 requires two
        approvals for a higher-risk transaction, one of them from Financial Control, and an
        earlier version of this method advanced to execution on the first signature whatever
        the requirement said. The requirement was computed, stored, shown to the approver, and
        read by nobody: two independent reviews posted a bank-change case on one approval.

        Returns the run and whether the delivery was a replay. A replay changes nothing and
        costs nothing: it is detected before authority is validated or any tool is called.
        """
        state = self._repository.require_run(run_id)
        emitter = self._emitter(state)

        if state.approval_id is None:
            raise ApprovalStateConflict(f"run {run_id} has no approval to resolve")
        if decision.approval_id != state.approval_id:
            raise ApprovalStateConflict(
                f"approval {decision.approval_id} does not belong to run {run_id}"
            )

        approval = self._repository.load_approval(state.approval_id)
        if approval is None:
            raise ApprovalStateConflict(f"approval {state.approval_id} is missing")

        # A settled approval short-circuits before anything else happens. Checked here rather
        # than relying on the store's idempotency alone, because everything below costs
        # something: re-validating authority reads the authority register again, spending tool
        # budget on a question already answered, and re-deriving the verdict invites a
        # different answer if the register changed in between. A duplicate delivery must be
        # inert, not merely harmless.
        replay = short_circuit_replay(self._repository, state, emitter, approval, decision, status)
        if replay is not None:
            return replay

        # Authority is validated before a signature is accepted, not after. An approver
        # without sufficient authority has not approved anything, so recording the signature
        # and checking afterwards would leave one on file that was never valid.
        delegation = self._load_delegation(state, emitter, decision)
        validation, segregation = authorise_signature(
            approval=approval,
            decision=decision,
            state=state,
            delegation=delegation,
            as_of=self._clock(),
        )

        if status is ApprovalStatus.APPROVED and not (
            validation.sufficient and segregation.satisfied
        ):
            reasons = [
                *validation.reasons,
                *(exception.observed for exception in segregation.exceptions),
            ]
            state.add_exceptions([*validation.exceptions, *segregation.exceptions])
            state.add_findings(list(validation.findings))
            emitter.emit(
                EventType.APPROVAL_RESOLVED,
                payload={
                    "approval_id": approval.approval_id,
                    "decision": "REFUSED",
                    "approver_id": decision.approver_id,
                    "approver_role": decision.approver_role,
                    "reasons": reasons,
                },
                phase=state.phase,
                outcome="DENIED",
            )
            state = self._repository.save_run(state)
            raise ApprovalStateConflict(
                "the approver may not approve this transaction: " + "; ".join(reasons)
            )

        if status is ApprovalStatus.REJECTED:
            resolved, replayed = self._repository.reject_approval(
                approval.approval_id,
                decided_by=decision.approver_id,
                decided_by_role=decision.approver_role,
                comment=decision.comment,
                decided_at=self._clock(),
            )
        else:
            resolved, replayed = self._repository.add_approval_signature(
                approval.approval_id,
                ApprovalSignature(
                    approver_id=decision.approver_id,
                    approver_role=decision.approver_role,
                    effective_role=(
                        validation.effective_role.value if validation.effective_role else ""
                    ),
                    applicable_limit=validation.applicable_limit,
                    authority_register_version=validation.authority_register_version,
                    delegation_applied=validation.delegation_applied,
                    is_financial_control=validation.is_financial_control,
                    signed_at=self._clock(),
                    comment=decision.comment,
                ),
            )

        if replayed:
            emitter.emit(
                EventType.APPROVAL_REPLAYED,
                payload={
                    "approval_id": resolved.approval_id,
                    "status": resolved.status.value,
                    "approver_id": decision.approver_id,
                    "signatures_collected": resolved.signatures_collected,
                },
                phase=state.phase,
                outcome="REPLAYED",
            )
            current = self._repository.require_run(run_id)
            if current.phase.is_terminal:
                return current, True
            state = current
        else:
            emitter.emit(
                EventType.APPROVAL_RESOLVED,
                payload={
                    "approval_id": resolved.approval_id,
                    "decision": status.value,
                    "approver_id": decision.approver_id,
                    "approver_role": decision.approver_role,
                    "effective_role": (
                        validation.effective_role.value if validation.effective_role else None
                    ),
                    "applicable_limit": (
                        str(validation.applicable_limit) if validation.applicable_limit else None
                    ),
                    "authority_register_version": validation.authority_register_version,
                    "delegation_applied": validation.delegation_applied,
                    "is_financial_control": validation.is_financial_control,
                    "signatures_collected": resolved.signatures_collected,
                    "signatures_required": resolved.required_signature_count,
                    "requirement_met": resolved.signature_requirement_met,
                },
                phase=state.phase,
                outcome=status.value,
            )
            state.add_findings([*validation.findings, *segregation.findings])

        # The gate stays closed until every required signature is collected. This is the
        # branch that was missing: an earlier version advanced to EXECUTE_DECISION on the
        # first signature whatever the requirement said.
        if status is ApprovalStatus.APPROVED and not resolved.signature_requirement_met:
            state = self._repository.save_run(state)
            emitter.emit(
                EventType.APPROVAL_REQUESTED,
                payload={
                    "approval_id": resolved.approval_id,
                    "signatures_collected": resolved.signatures_collected,
                    "signatures_required": resolved.required_signature_count,
                    "outstanding": resolved.outstanding_requirement_detail(),
                },
                phase=state.phase,
                outcome="AWAITING_FURTHER_SIGNATURE",
            )
            # ``replayed``, not False. An earlier version hardcoded False here and a
            # regression test caught it: a duplicate delivery from an approver who had
            # already signed was reported as a fresh signature, even though the store had
            # correctly ignored it. A caller retrying a delivery would have read that as
            # progress towards the second signature.
            return state, replayed

        if status is ApprovalStatus.REJECTED:
            state.actions_taken.append(
                ActionRecord(
                    action="APPROVAL_REJECTED",
                    target="accounts payable case file",
                    performed_at=self._clock(),
                    reference=resolved.approval_id,
                    simulated=True,
                    detail=(
                        f"{resolved.decided_by} ({resolved.decided_by_role}) declined the "
                        f"recommendation to {resolved.requested_outcome.value}. Nothing was "
                        "posted."
                    ),
                )
            )
            state.phase = RunPhase.HELD
            state.status = RunStatus.HELD
            state = self._repository.save_run(state)
            self._finalise(state, emitter)
            return state, replayed

        state.phase = RunPhase.EXECUTE_DECISION
        state.status = RunStatus.RUNNING
        state = self._repository.save_run(state)
        return self._drive(state, emitter), replayed

    def _load_delegation(
        self, state: RunState, emitter: EventEmitter, decision: ApprovalDecision
    ) -> DelegationRecord | None:
        """Fetch a delegation, only when the callback names one."""
        if not decision.delegation_id:
            return None
        runner = ToolRunner(
            emitter=emitter,
            max_tool_calls=self._budget.max_tool_calls,
            calls_already_used=state.tool_calls_used,
        )
        result = runner.run(
            GET_AUTHORITY_DELEGATION,
            get_authority_delegation(self._backends(state)),  # type: ignore[arg-type]
            GetDelegationInput(delegation_id=decision.delegation_id),
            GetDelegationOutput,
        )
        state.tool_calls_used = runner.calls_used
        if result.succeeded and result.value is not None:
            state.delegation = result.value.delegation
            return result.value.delegation
        state.add_unknowns(
            [
                Unknown(
                    item=f"authority register entry {decision.delegation_id}",
                    reason=f"{GET_AUTHORITY_DELEGATION.name} failed: {result.error_message}",
                    impact="the delegation could not be validated, so it was not applied",
                    how_to_resolve="retry once the authority register is reachable",
                    source_attempted=GET_AUTHORITY_DELEGATION.name,
                )
            ]
        )
        return None

    # ---- model plumbing ----------------------------------------------------------------

    def call_model[T: BaseModel](
        self, state: RunState, emitter: EventEmitter, prompt: str, schema: type[T]
    ) -> T:
        """Call the model and record the call, whether it succeeded or not.

        Generic over the schema rather than returning ``Any``. When the phases lived on this
        class the looser type cost nothing, because every call site was a few lines away. Now
        that a phase reaches this through a protocol, ``Any`` would erase the schema at the one
        boundary where a reader most wants to know what came back, and every field access in a
        phase would be unchecked.
        """
        started = time.perf_counter()
        try:
            value, record = self._llm.complete_structured(
                system=SYSTEM_PROMPT,
                user=prompt,
                schema=schema,
                max_tokens=self._settings.llm_max_tokens,
            )
        except ModelOutputInvalid as error:
            emitter.model_call(
                provider=self._llm.provider_name,
                model=self._llm.model_name,
                schema_name=schema.__name__,
                outcome="INVALID_OUTPUT",
                duration_ms=int((time.perf_counter() - started) * 1000),
                attempt=2,
                validation_error=error.validation_error,
            )
            raise
        except ModelError as error:
            emitter.model_call(
                provider=self._llm.provider_name,
                model=self._llm.model_name,
                schema_name=schema.__name__,
                outcome="UNAVAILABLE",
                duration_ms=int((time.perf_counter() - started) * 1000),
                attempt=1,
                validation_error=str(error),
            )
            raise
        emitter.model_call(
            provider=record.provider,
            model=record.model,
            schema_name=record.schema_name,
            outcome="REPAIRED" if record.repaired else "SUCCESS",
            duration_ms=int((time.perf_counter() - started) * 1000),
            attempt=record.attempts,
            input_tokens=record.input_tokens,
            output_tokens=record.output_tokens,
        )
        return value

    # ---- summaries supplied to the model ------------------------------------------------

    # ---- terminal handling --------------------------------------------------------------

    def _fail(
        self,
        state: RunState,
        emitter: EventEmitter,
        reason: FailureReason,
        detail: str,
    ) -> RunState:
        """Put the run into an explicit failure state.

        A failed run keeps everything it established. The partial result is what lets a human
        see how far the run got and resume from there, which FIN-POL-007 §5 requires.
        """
        state.phase = RunPhase.FAILED
        state.status = RunStatus.FAILED
        state.failure_reason = reason
        state.failure_detail = detail[:2_000]
        state = self._repository.save_run(state)
        emitter.emit(
            EventType.RUN_FAILED,
            payload={"reason": reason.value, "detail": state.failure_detail},
            phase=RunPhase.FAILED,
            outcome="FAILED",
        )
        self._finalise(state, emitter)
        return state

    def _finalise(self, state: RunState, emitter: EventEmitter) -> None:
        """Emit the completion event. The typed result is assembled on demand by
        ``build_final_result`` so that a failed run still has one."""
        emitter.emit(
            EventType.RUN_COMPLETED,
            payload={
                "status": state.status.value,
                "outcome": state.recommendation.outcome.value if state.recommendation else None,
                "steps_used": state.steps_used,
                "tool_calls_used": state.tool_calls_used,
                "exception_count": len(state.exceptions),
                "unknown_count": len(state.unknowns),
                "action_count": len(state.actions_taken),
                "decision_ref": state.decision.decision_ref if state.decision else None,
            },
            phase=state.phase,
            outcome=state.status.value,
        )


def build_final_result(state: RunState) -> FinalResult | None:
    """Assemble the typed audit record from a run's state.

    Returns ``None`` only when the run has no recommendation at all, which happens when it
    failed before reaching ``RECOMMEND``. The caller reports the failure reason in that case
    rather than an empty result that looks like a conclusion.
    """
    if state.recommendation is None:
        return None
    return FinalResult(
        case_id=state.case_id,
        run_id=state.run_id,
        sourced_facts=list(state.sourced_facts),
        calculations=list(state.calculations),
        inferences=list(state.inferences),
        unknowns=list(state.unknowns),
        policy_findings=list(state.policy_findings),
        actions_taken=list(state.actions_taken),
        recommendation=state.recommendation,
        failure_reason=state.failure_reason,
        completed_at=state.updated_at if state.is_terminal else None,
    )


def summarise_run(state: RunState) -> dict[str, Any]:
    """A compact view for a report table or a log line."""
    return {
        "run_id": state.run_id,
        "case_id": state.case_id,
        "status": state.status.value,
        "phase": state.phase.value,
        "outcome": state.recommendation.outcome.value if state.recommendation else None,
        "steps_used": state.steps_used,
        "tool_calls_used": state.tool_calls_used,
        "exceptions": sorted({exception.category.value for exception in state.exceptions}),
        "indicators": [indicator.code for indicator in state.fraud_indicators],
        "unknowns": len(state.unknowns),
        "decision_ref": state.decision.decision_ref if state.decision else None,
        "failure_reason": state.failure_reason.value if state.failure_reason else None,
    }


__all__ = ["Clock", "Orchestrator", "build_final_result", "summarise_run"]
