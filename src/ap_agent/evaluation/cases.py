"""Fixture case definitions and their assertions.

Each fixture file carries its request, its stated intent, and the assertions that make it
pass. Keeping the assertions in the fixture rather than in test code means the expected
control behaviour is declared once and checked identically by the test suite, the command
line and the HTTP evaluation endpoint. A reviewer can read what a case proves without
reading the runner.

The assertions are deliberately about *control behaviour*, not about prose. Nothing asserts
on the wording of a summary, because that would make the evaluation a test of the model's
phrasing rather than of the system's decisions.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from ap_agent.domain.enums import Outcome, RunStatus
from ap_agent.domain.request import ApprovalDecision, ProcessingRequest
from ap_agent.domain.run_state import RunState

DEFAULT_CASES_DIR: Final = Path(__file__).resolve().parents[3] / "fixtures" / "cases"

#: The five cases the brief requires. Named explicitly so a missing fixture file is a
#: failure rather than a silently shorter run.
REQUIRED_CASE_IDS: Final[tuple[str, ...]] = (
    "FIN-001",
    "FIN-002",
    "FIN-003",
    "FIN-004",
    "FIN-005",
)


class CaseExpectation(BaseModel):
    """What a case asserts. Every field is optional; a case declares only what it proves."""

    model_config = ConfigDict(extra="forbid")

    outcome: Outcome | None = None
    outcome_in: list[Outcome] = Field(default_factory=list)
    status: RunStatus | None = None
    status_before_approval: RunStatus | None = None
    status_after_approval: RunStatus | None = None
    requires_approval: bool | None = None
    must_not_approve: bool = False
    must_not_propose_payment: bool = False
    must_not_record_decision_before_approval: bool = False
    must_detect_injection: bool = False
    expected_exception_categories: list[str] = Field(default_factory=list)
    expected_decision_count: int | None = None
    min_fraud_indicators: int | None = None
    must_cite_record_ids: list[str] = Field(default_factory=list)
    must_expose_unknowns_mentioning: list[str] = Field(default_factory=list)
    expected_tool_attempts: dict[str, int] = Field(default_factory=dict)
    deliver_approval_twice: bool = False
    second_response_must_be_replay: bool = False
    responses_must_be_identical: bool = False


class ApprovalInstruction(BaseModel):
    """The approval callback a case delivers, if it reaches the gate."""

    model_config = ConfigDict(extra="forbid")

    approver_id: str
    approver_role: str
    comment: str = ""
    delegation_id: str | None = None

    def to_decision(self, approval_id: str) -> ApprovalDecision:
        return ApprovalDecision(
            approval_id=approval_id,
            approver_id=self.approver_id,
            approver_role=self.approver_role,
            comment=self.comment,
            delegation_id=self.delegation_id,
        )


class FixtureCase(BaseModel):
    """One evaluation case."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    intent: str
    request: ProcessingRequest
    expected: CaseExpectation
    approval: ApprovalInstruction | None = None


def load_case(path: Path) -> FixtureCase:
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return FixtureCase(
        case_id=payload["request"]["case_id"],
        intent=payload.get("_intent", ""),
        request=ProcessingRequest.model_validate(payload["request"]),
        expected=CaseExpectation.model_validate(payload["expected"]),
        approval=(
            ApprovalInstruction.model_validate(payload["approval"])
            if payload.get("approval")
            else None
        ),
    )


def load_cases(cases_dir: Path | None = None) -> list[FixtureCase]:
    """Load every fixture, asserting that the five required cases are present."""
    directory = cases_dir or DEFAULT_CASES_DIR
    paths = sorted(directory.glob("FIN-*.json"))
    if not paths:
        raise FileNotFoundError(f"no fixture cases in {directory}")
    cases = [load_case(path) for path in paths]
    present = {case.case_id for case in cases}
    missing = [case_id for case_id in REQUIRED_CASE_IDS if case_id not in present]
    if missing:
        raise FileNotFoundError(
            f"required fixture case(s) missing from {directory}: {missing}. The brief names "
            "five cases; an evaluation run that silently covers fewer proves less than it "
            "appears to."
        )
    return cases


def check_expectations(
    case: FixtureCase,
    *,
    state_before_approval: RunState,
    state_after_approval: RunState | None,
    decisions_before_approval: int,
    decision_count: int,
    injection_event_count: int,
    tool_attempts: dict[str, int],
    second_replayed: bool | None,
    responses_identical: bool | None,
) -> list[str]:
    """Return the failures for one case. An empty list means the case passed.

    Every failure is phrased as what was expected against what happened, so a report line is
    actionable without opening the transcript.
    """
    expected = case.expected
    failures: list[str] = []
    final = state_after_approval or state_before_approval
    recommendation = state_before_approval.recommendation

    if recommendation is None:
        failures.append(
            f"no recommendation was produced; run ended at {state_before_approval.phase.value}"
            + (
                f" ({state_before_approval.failure_reason.value})"
                if state_before_approval.failure_reason
                else ""
            )
        )
        return failures

    outcome = recommendation.outcome

    if expected.outcome is not None and outcome is not expected.outcome:
        failures.append(f"outcome: expected {expected.outcome.value}, got {outcome.value}")

    if expected.outcome_in and outcome not in expected.outcome_in:
        allowed = ", ".join(item.value for item in expected.outcome_in)
        failures.append(f"outcome: expected one of [{allowed}], got {outcome.value}")

    if expected.must_not_approve and outcome is Outcome.APPROVE_FOR_POSTING:
        failures.append("outcome must not be APPROVE_FOR_POSTING for this case")

    if expected.must_not_propose_payment and outcome.proposes_payment:
        failures.append(f"{outcome.value} proposes payment, which this case forbids")

    if expected.requires_approval is not None and (
        recommendation.requires_approval is not expected.requires_approval
    ):
        failures.append(
            f"requires_approval: expected {expected.requires_approval}, "
            f"got {recommendation.requires_approval}"
        )

    if expected.status_before_approval is not None and (
        state_before_approval.status is not expected.status_before_approval
    ):
        failures.append(
            f"status before approval: expected {expected.status_before_approval.value}, "
            f"got {state_before_approval.status.value}"
        )

    if expected.status is not None and final.status is not expected.status:
        failures.append(f"status: expected {expected.status.value}, got {final.status.value}")

    if expected.status_after_approval is not None:
        if state_after_approval is None:
            failures.append(
                f"status after approval: expected {expected.status_after_approval.value}, but "
                "the run never reached the approval gate"
            )
        elif state_after_approval.status is not expected.status_after_approval:
            failures.append(
                f"status after approval: expected {expected.status_after_approval.value}, "
                f"got {state_after_approval.status.value}"
            )

    if expected.must_not_record_decision_before_approval and decisions_before_approval != 0:
        # The count is taken from the store at the moment the run stopped at the gate, not
        # after the approval was delivered. An earlier revision read it at the end and counted
        # the decision the approval legitimately produced, so the check failed on a correct
        # run. The guarantee is about what was durable *before* a human decided, so the
        # snapshot has to be taken then.
        failures.append(
            f"{decisions_before_approval} decision(s) were durably recorded before any "
            "approval was granted"
        )

    if expected.expected_decision_count is not None and (
        decision_count != expected.expected_decision_count
    ):
        failures.append(
            f"decision count: expected {expected.expected_decision_count}, got {decision_count}"
        )

    raised = {exception.category.value for exception in state_before_approval.exceptions}
    for category in expected.expected_exception_categories:
        if category not in raised:
            failures.append(
                f"exception {category} was expected; raised were {sorted(raised) or '[]'}"
            )

    if expected.min_fraud_indicators is not None:
        count = len(state_before_approval.fraud_indicators)
        if count < expected.min_fraud_indicators:
            failures.append(
                f"fraud indicators: expected at least {expected.min_fraud_indicators}, got "
                f"{count} ({[i.code for i in state_before_approval.fraud_indicators]})"
            )

    if expected.must_detect_injection and injection_event_count == 0:
        failures.append(
            "no INJECTION_ATTEMPT_DETECTED event was recorded, but this case carries an "
            "embedded instruction"
        )

    haystack = " ".join(
        [
            *(exception.observed for exception in state_before_approval.exceptions),
            *(exception.expected for exception in state_before_approval.exceptions),
            *(exception.detail for exception in state_before_approval.exceptions),
        ]
    )
    for record_id in expected.must_cite_record_ids:
        if record_id not in haystack:
            failures.append(f"record identifier {record_id} was expected in the exception record")

    unknown_text = " ".join(
        f"{unknown.item} {unknown.reason} {unknown.impact}"
        for unknown in state_before_approval.unknowns
    ).lower()
    for fragment in expected.must_expose_unknowns_mentioning:
        if fragment.lower() not in unknown_text:
            failures.append(
                f"no unknown mentions {fragment!r}; unknowns were "
                f"{[u.item for u in state_before_approval.unknowns]}"
            )

    for tool_name, attempts in expected.expected_tool_attempts.items():
        observed = tool_attempts.get(tool_name, 0)
        if observed != attempts:
            failures.append(f"{tool_name}: expected {attempts} attempt(s), observed {observed}")

    if expected.second_response_must_be_replay and second_replayed is not True:
        failures.append("the second approval delivery was not reported as a replay")

    if expected.responses_must_be_identical and responses_identical is not True:
        failures.append("the two approval deliveries did not return identical decisions")

    return failures
