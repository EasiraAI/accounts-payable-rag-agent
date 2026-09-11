"""HTTP surface.

Four mandatory operations plus two diagnostics. The handlers are thin by design: each
validates its input, calls the orchestrator, and maps a domain error to a status code. No
control logic lives here, because a control that exists only on the HTTP path is one the CLI
and the evaluation runner do not have.

## Status-code choices

``409 Conflict`` for an approval-state conflict. The request was well formed and the caller
was authorised; the resource is simply not in a state that permits the operation. That
distinguishes "you asked at the wrong time" from "you asked wrongly", which matters to a
caller deciding whether to retry.

``422`` comes from FastAPI's own validation, so a malformed request never reaches a handler.

``200`` for a replayed approval, not ``409``. A duplicate delivery is a success: the caller
asked for a state that already holds, and the response body is identical to the first. A
conflict status would push a well-behaved caller into retrying or alerting on an event that
is entirely normal in an at-least-once delivery system.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from ap_agent.composition import Application, build_application
from ap_agent.domain.errors import (
    ApprovalStateConflict,
    IndexNotBuilt,
    ModelError,
    RunNotFound,
)
from ap_agent.domain.request import ApprovalDecision, ProcessingRequest
from ap_agent.domain.results import RunView
from ap_agent.domain.run_state import RunState
from ap_agent.evaluation.retrieval import evaluate_retrieval, load_golden_set
from ap_agent.evaluation.runner import EvaluationReport, run_evaluation
from ap_agent.orchestration.machine import build_final_result, summarise_run
from ap_agent.tools.base import describe_tools
from ap_agent.tools.contracts import ALL_TOOL_SPECS

_GOLDEN_SET_PATH_CANDIDATES = ("tests/eval/retrieval_golden.json",)


class ApprovalBody(BaseModel):
    """Approve or reject payload.

    Mirrors ``ApprovalDecision`` rather than reusing it so the wire format can evolve
    separately from the domain type. ``approval_id`` is required: an approval callback that
    does not name what it is approving cannot be matched to a pending decision, and accepting
    one would mean guessing.
    """

    model_config = ConfigDict(extra="forbid")

    approval_id: Annotated[str, Field(min_length=1, max_length=256)]
    approver_id: Annotated[str, Field(min_length=1, max_length=256)]
    approver_role: Annotated[str, Field(min_length=1, max_length=128)]
    comment: Annotated[str, Field(max_length=2_000)] = ""
    delegation_id: str | None = None

    def to_decision(self) -> ApprovalDecision:
        return ApprovalDecision(
            approval_id=self.approval_id,
            approver_id=self.approver_id,
            approver_role=self.approver_role,
            comment=self.comment,
            delegation_id=self.delegation_id,
        )


class ApprovalResponse(BaseModel):
    """The result of an approve or reject call.

    ``replayed`` tells a caller whether its delivery was the effective one. The rest of the
    body is identical either way, which is what makes the callback safe to retry.
    """

    model_config = ConfigDict(extra="forbid")

    replayed: bool
    #: Whether every required signature is now collected. False means the gate is still
    #: closed and a further, different approver must sign (FIN-POL-003 §3).
    signature_requirement_met: bool = True
    signatures_collected: int = 0
    signatures_required: int = 1
    outstanding_requirement: str = ""
    run: RunView


def _to_view(state: RunState, application: Application) -> RunView:
    """Render a run for the wire, including its full event log.

    The events are included rather than left to a second endpoint. The brief asks for a run
    to be explainable, and an explanation assembled by the reader from two calls is one they
    have to assemble.
    """
    events = application.repository.list_events(state.run_id)
    pending = (
        application.repository.find_pending_approval(state.run_id) if state.approval_id else None
    )
    return RunView(
        run_id=state.run_id,
        case_id=state.case_id,
        status=state.status,
        phase=state.phase,
        created_at=state.created_at,
        updated_at=state.updated_at,
        steps_used=state.steps_used,
        tool_calls_used=state.tool_calls_used,
        recommendation=state.recommendation,
        result=build_final_result(state),
        pending_approval=pending,
        decision=state.decision,
        failure_reason=state.failure_reason,
        events=[
            {
                "sequence": event.sequence,
                "event_type": event.event_type,
                "phase": event.phase,
                "outcome": event.outcome,
                "duration_ms": event.duration_ms,
                "created_at": event.created_at.isoformat(),
                "payload": event.payload,
            }
            for event in events
        ],
    )


def _approval_response(
    state: RunState, application: Application, *, replayed: bool
) -> ApprovalResponse:
    """Build the approve/reject response, including how far the signature set has got.

    A caller that delivers the first of two required signatures needs to know the gate is
    still closed. Returning only the run would leave them to infer it from the status, and
    "AWAITING_APPROVAL after a successful approval" is exactly the sort of thing a caller
    misreads as a failure.
    """
    approval = (
        application.repository.load_approval(state.approval_id) if state.approval_id else None
    )
    return ApprovalResponse(
        replayed=replayed,
        signature_requirement_met=(
            approval.signature_requirement_met if approval is not None else True
        ),
        signatures_collected=approval.signatures_collected if approval is not None else 0,
        signatures_required=approval.required_signature_count if approval is not None else 1,
        outstanding_requirement=(
            approval.outstanding_requirement_detail() if approval is not None else ""
        ),
        run=_to_view(state, application),
    )


def get_application(request: Request) -> Application:
    """Dependency accessor for the assembled system.

    Built once at startup and stored on the app state, so the index is not rebuilt and the
    database is not reopened per request.
    """
    application = getattr(request.app.state, "application", None)
    # isinstance rather than a None check: the attribute is untyped application state, and
    # narrowing it here means the handlers below receive a known type rather than Any.
    if not isinstance(application, Application):  # pragma: no cover - guarded by the factory
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="the application is not initialised",
        )
    return application


def create_app(application: Application | None = None) -> FastAPI:
    """Build the HTTP application.

    Accepting a pre-built ``Application`` is what lets the contract tests run against a
    temporary database and the deterministic model adapter without patching anything.
    """

    @asynccontextmanager
    async def lifespan(instance: FastAPI) -> AsyncIterator[None]:
        """Assemble the system once at startup and release it at shutdown.

        The lifespan API rather than the deprecated ``on_event`` hooks. Building here rather
        than per request means the index is loaded and the database opened once; closing here
        means a reloading development server does not leak connections.

        An application supplied by a caller is left alone: the tests own its lifetime, and
        closing a connection the test still needs would be worse than leaving it open.
        """
        owned = instance.state.application is None
        if owned:
            instance.state.application = build_application()
        try:
            yield
        finally:
            if owned and instance.state.application is not None:
                instance.state.application.close()

    app = FastAPI(
        title="Accounts-Payable Processing Agent",
        version="1.0.0",
        description=(
            "Reconciles invoice evidence against policy, produces a cited recommendation, "
            "and stops at a human approval gate before recording any consequential outcome. "
            "All posting is simulated; this service has no payment rail."
        ),
        lifespan=lifespan,
    )
    app.state.application = application

    # ---- error mapping ---------------------------------------------------------------

    @app.exception_handler(RunNotFound)
    def _run_not_found(_request: Request, error: RunNotFound) -> Any:
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=404, content={"detail": str(error)})

    @app.exception_handler(ApprovalStateConflict)
    def _approval_conflict(_request: Request, error: ApprovalStateConflict) -> Any:
        from fastapi.responses import JSONResponse

        # 409, not 400: the request was well formed, the resource is in the wrong state.
        return JSONResponse(status_code=409, content={"detail": str(error)})

    @app.exception_handler(ModelError)
    def _model_error(_request: Request, error: ModelError) -> Any:
        from fastapi.responses import JSONResponse

        # 502: the failure is in an upstream dependency, not in the caller's request.
        return JSONResponse(status_code=502, content={"detail": str(error)})

    @app.exception_handler(IndexNotBuilt)
    def _index_missing(_request: Request, error: IndexNotBuilt) -> Any:
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=503, content={"detail": str(error)})

    # ---- operations -------------------------------------------------------------------

    @app.post("/runs", response_model=RunView, status_code=status.HTTP_201_CREATED)
    def start_run(
        request: ProcessingRequest,
        application: Annotated[Application, Depends(get_application)],
    ) -> RunView:
        """Start a run and execute it until completion, failure, or the approval gate.

        Synchronous. A run is bounded by its step and tool budgets and completes in
        milliseconds against simulated backends, so returning the finished state is more
        useful than handing back a job identifier the caller must poll.
        """
        state = application.orchestrator.start(request)
        return _to_view(state, application)

    @app.get("/runs/{run_id}", response_model=RunView)
    def get_run(
        run_id: str,
        application: Annotated[Application, Depends(get_application)],
    ) -> RunView:
        """Return status, current state, result and audit events for a run."""
        state = application.repository.require_run(run_id)
        return _to_view(state, application)

    @app.get("/runs")
    def list_runs(
        application: Annotated[Application, Depends(get_application)],
        limit: Annotated[int, Query(ge=1, le=200)] = 25,
    ) -> list[dict[str, object]]:
        """Recent runs, newest first, as compact summaries."""
        return [summarise_run(state) for state in application.repository.list_runs(limit=limit)]

    @app.post("/runs/{run_id}/approve", response_model=ApprovalResponse)
    def approve_run(
        run_id: str,
        body: ApprovalBody,
        application: Annotated[Application, Depends(get_application)],
    ) -> ApprovalResponse:
        """Approve a pending decision and resume the run.

        Idempotent. A duplicate delivery returns 200 with an identical body and
        ``replayed: true``; it records no second decision.
        """
        state, replayed = application.orchestrator.approve(run_id, body.to_decision())
        return _approval_response(state, application, replayed=replayed)

    @app.post("/runs/{run_id}/reject", response_model=ApprovalResponse)
    def reject_run(
        run_id: str,
        body: ApprovalBody,
        application: Annotated[Application, Depends(get_application)],
    ) -> ApprovalResponse:
        """Reject a pending decision. Nothing is posted and the case is held."""
        state, replayed = application.orchestrator.reject(run_id, body.to_decision())
        return _approval_response(state, application, replayed=replayed)

    @app.get("/evaluations", response_model=EvaluationReport)
    def list_evaluation_results(
        application: Annotated[Application, Depends(get_application)],
    ) -> EvaluationReport:
        """Run the fixture cases and report pass or fail per case.

        Always uses the deterministic adapter, whatever the service is configured with. An
        evaluation endpoint that consumed live model quota on each call would be used once.
        """
        return run_evaluation(settings=application.settings)

    # ---- diagnostics -------------------------------------------------------------------

    @app.get("/health")
    def health(
        application: Annotated[Application, Depends(get_application)],
    ) -> dict[str, object]:
        """Liveness plus what this instance is made of."""
        return {"status": "ok", **application.describe()}

    @app.get("/manifest")
    def manifest(
        application: Annotated[Application, Depends(get_application)],
    ) -> dict[str, object]:
        """The component manifest, generated from the running configuration.

        Generated rather than written down, so it cannot describe a system other than the one
        serving the request.
        """
        from pathlib import Path

        golden: dict[str, object] = {}
        for candidate in _GOLDEN_SET_PATH_CANDIDATES:
            path = Path(candidate)
            if path.is_file():
                report = evaluate_retrieval(
                    application.retriever,
                    load_golden_set(path),
                    top_k=application.settings.retrieval_top_k,
                )
                golden = {
                    "queries": report.query_count,
                    "hit_at_1": round(report.hit_at_1, 3),
                    "hit_at_3": round(report.hit_at_3, 3),
                    "mean_reciprocal_rank": round(report.mean_reciprocal_rank, 3),
                    "distractor_leaks": len(report.distractor_leaks),
                    "stale_policy_leaks": len(report.stale_policy_leaks),
                }
                break

        return {
            "runtime": {
                "agent_runtime": "framework-free explicit state machine (ap_agent.orchestration)",
                "api": "FastAPI",
                "persistence": "SQLite (WAL)",
                "document_store": (
                    f"BM25 over section chunks ({application.settings.retrieval_mode})"
                ),
            },
            "configuration": application.describe(),
            "tools": describe_tools(ALL_TOOL_SPECS),
            "trust_boundaries": {
                "trusted_policy": [
                    "system prompt",
                    "orchestration code",
                    "deterministic rule engine",
                    "tool permission table",
                ],
                "untrusted_evidence": [
                    "retrieved corpus chunks",
                    "vendor, purchase-order and history records",
                ],
                "untrusted_case_input": ["request notes", "request attachments"],
                "enforcement": [
                    "the model cannot call the write tool",
                    "the outcome is computed by the rule engine from typed facts",
                    "a model suggestion may only make an outcome more conservative",
                    "no tool argument widens permission",
                    "citations are resolved against what was retrieved",
                    "one redaction path for every event and log line",
                ],
            },
            "retrieval_quality": golden,
        }

    return app


#: Module-level application for ``uvicorn ap_agent.api.app:app``.
app = create_app()
