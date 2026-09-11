"""Evaluation runner.

Executes the fixture cases end to end and reports pass or fail per case. The same runner
backs the ``eval`` command and the ``GET /evaluations`` endpoint, so the numbers a reviewer
sees from either are produced by the same code.

Each case runs against its own temporary database. Two reasons, both practical: a case must
not be able to pass because a previous case left a row behind, and the idempotency
constraints are per-run, so sharing a store would let one case's decision block another's.
Retrieval is shared, because the index is read-only and rebuilding it per case would be
slower without testing anything extra.
"""

from __future__ import annotations

import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ap_agent.config.settings import Settings
from ap_agent.domain.enums import EventType
from ap_agent.domain.errors import ApprovalStateConflict
from ap_agent.domain.run_state import RunState
from ap_agent.evaluation.cases import FixtureCase, check_expectations, load_cases
from ap_agent.llm.base import LLMClient
from ap_agent.llm.fake_client import FakeLLMClient
from ap_agent.orchestration.machine import Orchestrator, build_final_result, summarise_run
from ap_agent.persistence.repository import Repository
from ap_agent.rag.index import CorpusIndex, load_or_build_index
from ap_agent.rag.retriever import Retriever

#: Fixed clock for evaluation runs. The fixtures use relative dates resolved against it, so
#: the cases keep their meaning while the results stay byte-comparable between runs.
EVALUATION_CLOCK: datetime = datetime(2026, 9, 11, 9, 30, tzinfo=UTC)


class CaseResult(BaseModel):
    """The outcome of one evaluated case."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    intent: str
    passed: bool
    expected: str
    actual: str
    status: str
    steps_used: int
    tool_calls_used: int
    duration_ms: int
    decision_count: int
    exception_categories: list[str] = Field(default_factory=list)
    fraud_indicators: list[str] = Field(default_factory=list)
    unknown_count: int = 0
    citation_count: int = 0
    #: Injection attempts found in content belonging to this case: the request notes and its
    #: attachments. These contribute to the FIN-POL-005 §3 indicator count.
    injection_events_case: int = 0
    #: Injection attempts found in retrieved corpus documents. Recorded as a corpus hygiene
    #: observation and deliberately excluded from this case's indicator count: a document
    #: merely present in the corpus says nothing about this transaction. Reported separately
    #: so the two are never confused when reading a report.
    injection_events_corpus: int = 0
    failures: list[str] = Field(default_factory=list)
    run_id: str = ""


class EvaluationReport(BaseModel):
    """The whole evaluation run."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    clock: datetime
    case_count: int
    passed_count: int
    results: list[CaseResult] = Field(default_factory=list)
    duration_ms: int = 0

    @property
    def all_passed(self) -> bool:
        return self.passed_count == self.case_count

    def table(self) -> str:
        """A fixed-width table for terminal output."""
        header = (
            f"{'case':<9} {'expected':<24} {'actual':<24} {'result':<6} "
            f"{'steps':>5} {'tools':>5} {'ms':>6}"
        )
        divider = "-" * len(header)
        rows = [
            f"{result.case_id:<9} {result.expected[:24]:<24} {result.actual[:24]:<24} "
            f"{'PASS' if result.passed else 'FAIL':<6} {result.steps_used:>5} "
            f"{result.tool_calls_used:>5} {result.duration_ms:>6}"
            for result in self.results
        ]
        summary = (
            f"{self.passed_count}/{self.case_count} cases passed "
            f"(provider={self.provider}, model={self.model})"
        )
        detail: list[str] = []
        for result in self.results:
            if result.failures:
                detail.append(f"\n{result.case_id} failures:")
                detail += [f"  - {failure}" for failure in result.failures]
        return "\n".join([header, divider, *rows, divider, summary, *detail])


def _expected_label(case: FixtureCase) -> str:
    expected = case.expected
    if expected.outcome is not None:
        return expected.outcome.value
    if expected.outcome_in:
        return " | ".join(item.value for item in expected.outcome_in)
    if expected.must_not_approve:
        return "not APPROVE"
    return "(behavioural)"


def evaluate_case(
    case: FixtureCase,
    *,
    settings: Settings,
    retriever: Retriever,
    llm_client: LLMClient,
    workspace: Path,
    clock: Callable[[], datetime],
    mock_data_dir: Path | None = None,
) -> tuple[CaseResult, RunState, Repository]:
    """Run one case and check its assertions.

    Returns the result along with the final state and the repository, so a caller can write a
    transcript without re-running the case.
    """
    started = time.perf_counter()
    case_settings = settings.model_copy(update={"db_path": workspace / f"{case.case_id}.db"})
    repository = Repository(case_settings.db_path)
    orchestrator = Orchestrator(
        settings=case_settings,
        repository=repository,
        retriever=retriever,
        llm_client=llm_client,
        clock=clock,
        mock_data_dir=mock_data_dir,
    )

    state_before = orchestrator.start(case.request)
    # Snapshot taken here, while the run is still stopped at the gate. Reading it after the
    # approval would count the decision the approval legitimately produced.
    decisions_before_approval = repository.count_decisions(state_before.run_id)
    state_after: RunState | None = None
    second_replayed: bool | None = None
    responses_identical: bool | None = None

    if case.approval is not None and state_before.approval_id:
        decision = case.approval.to_decision(state_before.approval_id)
        try:
            state_after, _ = orchestrator.approve(state_before.run_id, decision)
        except ApprovalStateConflict as error:
            # Recorded as a failure rather than raised: an approval refused for insufficient
            # authority is a legitimate outcome for some cases, and the assertions decide
            # whether it was the expected one.
            state_after = repository.require_run(state_before.run_id)
            state_after.failure_detail = str(error)
        if case.expected.deliver_approval_twice and state_after is not None:
            first_receipt = state_after.decision
            state_second, second_replayed = orchestrator.approve(state_before.run_id, decision)
            second_receipt = state_second.decision
            responses_identical = bool(
                first_receipt
                and second_receipt
                and first_receipt.decision_ref == second_receipt.decision_ref
                and first_receipt.idempotency_key == second_receipt.idempotency_key
                and first_receipt.amount == second_receipt.amount
                and first_receipt.outcome is second_receipt.outcome
            )
            state_after = state_second

    final = state_after or state_before
    events = repository.list_events(final.run_id)
    injection_case = 0
    injection_corpus = 0
    for event in events:
        if event.event_type != EventType.INJECTION_ATTEMPT_DETECTED.value:
            continue
        source = str(event.payload.get("source", ""))
        if source.startswith("document:"):
            injection_corpus += 1
        else:
            injection_case += 1
    tool_attempts: dict[str, int] = {}
    for event in events:
        if event.event_type == EventType.TOOL_CALL.value:
            name = str(event.payload.get("tool", ""))
            tool_attempts[name] = tool_attempts.get(name, 0) + 1

    failures = check_expectations(
        case,
        state_before_approval=state_before,
        state_after_approval=state_after,
        decisions_before_approval=decisions_before_approval,
        decision_count=repository.count_decisions(final.run_id),
        injection_event_count=injection_case + injection_corpus,
        tool_attempts=tool_attempts,
        second_replayed=second_replayed,
        responses_identical=responses_identical,
    )

    recommendation = final.recommendation
    result = CaseResult(
        case_id=case.case_id,
        intent=case.intent,
        passed=not failures,
        expected=_expected_label(case),
        actual=(
            recommendation.outcome.value
            if recommendation
            else f"{final.phase.value}"
            + (f"/{final.failure_reason.value}" if final.failure_reason else "")
        ),
        status=final.status.value,
        steps_used=final.steps_used,
        tool_calls_used=final.tool_calls_used,
        duration_ms=int((time.perf_counter() - started) * 1000),
        decision_count=repository.count_decisions(final.run_id),
        exception_categories=sorted({e.category.value for e in final.exceptions}),
        fraud_indicators=[indicator.code for indicator in final.fraud_indicators],
        unknown_count=len(final.unknowns),
        citation_count=len(recommendation.cited_evidence) if recommendation else 0,
        injection_events_case=injection_case,
        injection_events_corpus=injection_corpus,
        failures=failures,
        run_id=final.run_id,
    )
    return result, final, repository


def run_evaluation(
    *,
    settings: Settings,
    llm_client: LLMClient | None = None,
    cases_dir: Path | None = None,
    mock_data_dir: Path | None = None,
    workspace: Path | None = None,
    transcript_dir: Path | None = None,
) -> EvaluationReport:
    """Run every fixture case and return the report.

    ``transcript_dir`` writes a full per-case transcript, which is what the sample output in
    the documentation is generated from. Generating the samples from the same run that
    reports pass or fail means the documentation cannot describe behaviour the tests did not
    actually observe.
    """
    started = time.perf_counter()
    client = llm_client or FakeLLMClient()
    cases = load_cases(cases_dir)

    index: CorpusIndex = load_or_build_index(settings.corpus_dir, settings.index_dir)
    retriever = Retriever(
        index,
        superseded_score_factor=settings.superseded_score_factor,
        default_top_k=settings.retrieval_top_k,
    )

    root = workspace or Path(tempfile.mkdtemp(prefix="ap-eval-"))
    root.mkdir(parents=True, exist_ok=True)

    results: list[CaseResult] = []
    for case in cases:
        result, final, repository = evaluate_case(
            case,
            settings=settings,
            retriever=retriever,
            llm_client=client,
            workspace=root,
            clock=lambda: EVALUATION_CLOCK,
            mock_data_dir=mock_data_dir,
        )
        results.append(result)
        if transcript_dir is not None:
            write_transcript(transcript_dir, case, final, repository)
        repository.close()

    return EvaluationReport(
        provider=client.provider_name,
        model=client.model_name,
        clock=EVALUATION_CLOCK,
        case_count=len(results),
        passed_count=sum(1 for result in results if result.passed),
        results=results,
        duration_ms=int((time.perf_counter() - started) * 1000),
    )


def write_transcript(
    directory: Path,
    case: FixtureCase,
    state: RunState,
    repository: Repository,
) -> Path:
    """Write a full transcript for one case.

    Includes the event log, the recommendation and the typed final result. Everything written
    has already passed through redaction, because the events came from the store and the
    result holds no unmasked payment detail by construction.
    """
    import json

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{case.case_id}_transcript.json"
    final_result = build_final_result(state)
    payload: dict[str, Any] = {
        "case_id": case.case_id,
        "intent": case.intent,
        "summary": summarise_run(state),
        "request": case.request.model_dump(mode="json"),
        "recommendation": (
            state.recommendation.model_dump(mode="json") if state.recommendation else None
        ),
        "final_result": final_result.model_dump(mode="json") if final_result else None,
        "decision": state.decision.model_dump(mode="json") if state.decision else None,
        "events": [
            {
                "sequence": event.sequence,
                "event_type": event.event_type,
                "phase": event.phase,
                "outcome": event.outcome,
                "duration_ms": event.duration_ms,
                "payload": event.payload,
            }
            for event in repository.list_events(state.run_id)
        ],
    }
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    return path
