"""Output contracts.

The brief asks for two output shapes with overlapping but distinct field lists, and warns
against collapsing one into the other. They are modelled as two objects, both present on
the run result:

``Recommendation``  cited evidence, calculations, assumptions, confidence, exceptions,
                    next action. This is what an approver reads.
``FinalResult``     sourced facts, calculations, inferences, unknowns, policy findings,
                    actions taken. This is what an auditor reads, and it separates what was
                    retrieved from what was computed, what was inferred, and what remains
                    unknown.

Keeping them separate is not bookkeeping. The approver needs a decision and its basis; the
auditor needs the epistemic status of every statement. A single flat object would force one
audience to read the other's fields.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Final, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ap_agent.domain.enums import (
    ApprovalStatus,
    EscalationOwner,
    ExceptionCategory,
    FailureReason,
    Outcome,
    RunPhase,
    RunStatus,
)
from ap_agent.domain.evidence import Citation
from ap_agent.domain.money import MoneyAmount

NonEmptyStr = Annotated[str, Field(min_length=1, max_length=512)]
Confidence = Annotated[Decimal, Field(ge=0, le=1)]

#: Ceiling for narrative detail fields. Wider than ``NonEmptyStr`` because a finding's detail
#: may concatenate several reasons, and a rule engine that explains itself fully is worth more
#: than a tidier field.
MAX_DETAIL_LENGTH: Final = 2_000
DetailText = Annotated[str, Field(min_length=1, max_length=MAX_DETAIL_LENGTH)]


def truncate_detail(text: str) -> str:
    """Trim a detail string to the schema's own limit.

    Callers use this rather than their own slice length. An earlier revision truncated to a
    hand-written 1,200 characters while the field allowed 512, and a run with several
    recorded reasons failed validation mid-phase. Deriving the bound from the constant the
    annotation uses makes that divergence impossible.
    """
    if len(text) <= MAX_DETAIL_LENGTH:
        return text
    return text[: MAX_DETAIL_LENGTH - 3] + "..."


class Calculation(BaseModel):
    """An arithmetic step, recorded so it can be re-performed by hand.

    FIN-POL-002 §5 requires the calculation record to store input values, formula, result
    and rounding method. ``inputs`` holds rendered decimal strings rather than numbers so
    that the stored record is exact under JSON round-trip, where a float would not be.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: NonEmptyStr
    inputs: dict[str, str]
    formula: NonEmptyStr
    result: MoneyAmount
    currency: str | None = None
    rounding: str = "ROUND_HALF_UP"
    policy_ref: NonEmptyStr
    passed: bool | None = None
    note: str = ""


class ExceptionRecord(BaseModel):
    """A failed control.

    FIN-POL-007 §2 rejects generic notes: the record must name the rule, state expected and
    observed facts, cite sources, name an owner and set a review date. Each of those is a
    required field here, so an exception that cannot answer them cannot be constructed.
    """

    model_config = ConfigDict(extra="forbid")

    category: ExceptionCategory
    failed_rule: NonEmptyStr
    expected: NonEmptyStr
    observed: NonEmptyStr
    owner: EscalationOwner
    policy_refs: list[str] = Field(min_length=1)
    citations: list[Citation] = Field(default_factory=list)
    next_review_date: date | None = None
    detail: str = ""
    blocking: bool = True


class SourcedFact(BaseModel):
    """A statement taken from a document or a system of record, with its source.

    ``value`` is a string because a sourced fact may be a status, a date, an amount or a
    name. Typed values live on the evidence models; this is the audit rendering.
    """

    model_config = ConfigDict(extra="forbid")

    statement: NonEmptyStr
    value: str = ""
    source: NonEmptyStr
    citations: list[Citation] = Field(default_factory=list)


class Inference(BaseModel):
    """A conclusion the system drew that no single source states outright.

    Separated from sourced facts because the two carry different weight in a control review.
    ``basis`` must name what the inference was drawn from, and ``confidence`` is the model's
    own, recorded rather than acted upon: nothing in the decision path reads it.
    """

    model_config = ConfigDict(extra="forbid")

    statement: NonEmptyStr
    basis: NonEmptyStr
    confidence: Confidence = Decimal("0.5")
    citations: list[Citation] = Field(default_factory=list)


class Unknown(BaseModel):
    """Something the run could not establish.

    Recording unknowns explicitly is the mechanism that keeps missing evidence from being
    read as an absence of problems. ``impact`` states what the gap prevents, and
    ``how_to_resolve`` gives the approver an action rather than a complaint.
    """

    model_config = ConfigDict(extra="forbid")

    item: NonEmptyStr
    reason: NonEmptyStr
    impact: NonEmptyStr
    how_to_resolve: str = ""
    source_attempted: str = ""


class PolicyFinding(BaseModel):
    """The outcome of applying one policy rule, whether it passed or failed.

    Passing findings are retained deliberately. A record that shows which controls were
    evaluated and satisfied is what distinguishes a run that checked everything from a run
    that happened not to notice anything.
    """

    model_config = ConfigDict(extra="forbid")

    rule: NonEmptyStr
    policy_ref: NonEmptyStr
    satisfied: bool
    detail: DetailText
    citations: list[Citation] = Field(default_factory=list)


class ActionRecord(BaseModel):
    """Something the system actually did that had an effect outside itself."""

    model_config = ConfigDict(extra="forbid")

    action: NonEmptyStr
    target: NonEmptyStr
    performed_at: datetime
    reference: str = ""
    idempotency_key: str = ""
    simulated: bool = True
    detail: str = ""


class ConfidenceAssessment(BaseModel):
    """Confidence with its basis, rather than a bare number.

    A score on its own invites an approver to read it as a probability. Naming the drivers
    and the limits makes it a summary of evidence quality, which is what it is.
    """

    model_config = ConfigDict(extra="forbid")

    score: Confidence
    basis: NonEmptyStr
    drivers: list[str] = Field(default_factory=list)
    limits: list[str] = Field(default_factory=list)


class Recommendation(BaseModel):
    """What an approver is asked to decide on."""

    model_config = ConfigDict(extra="forbid")

    outcome: Outcome
    summary: NonEmptyStr
    cited_evidence: list[Citation] = Field(default_factory=list)
    calculations: list[Calculation] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    confidence: ConfidenceAssessment
    exceptions: list[ExceptionRecord] = Field(default_factory=list)
    next_action: NonEmptyStr
    requires_approval: bool
    requires_second_approval: bool = False
    second_approval_reason: str = ""

    @model_validator(mode="after")
    def _consequential_outcomes_require_approval(self) -> Self:
        """A consequential outcome must be marked as requiring approval.

        This is the schema-level half of the approval gate. The orchestrator enforces the
        same rule before acting, and the decision tool enforces it a third time against the
        stored approval record. Three independent checks, because a single point of
        enforcement for the property that keeps money from moving is not enough.
        """
        if self.outcome.is_consequential and not self.requires_approval:
            raise ValueError(
                f"outcome {self.outcome.value} is consequential under FIN-POL-001 §3 and "
                "must set requires_approval=True"
            )
        return self


class FinalResult(BaseModel):
    """The typed audit record of a run."""

    model_config = ConfigDict(extra="forbid")

    case_id: NonEmptyStr
    run_id: NonEmptyStr
    sourced_facts: list[SourcedFact] = Field(default_factory=list)
    calculations: list[Calculation] = Field(default_factory=list)
    inferences: list[Inference] = Field(default_factory=list)
    unknowns: list[Unknown] = Field(default_factory=list)
    policy_findings: list[PolicyFinding] = Field(default_factory=list)
    actions_taken: list[ActionRecord] = Field(default_factory=list)
    recommendation: Recommendation
    failure_reason: FailureReason | None = None
    completed_at: datetime | None = None

    @property
    def unresolved_controls(self) -> list[PolicyFinding]:
        return [finding for finding in self.policy_findings if not finding.satisfied]


class ApprovalRequest(BaseModel):
    """A pending human decision.

    ``presented_*`` fields exist because FIN-POL-003 §5 requires the approver to see the
    amount, vendor, exceptions and citations before deciding. Storing what was presented,
    rather than only what was decided, means the record shows the decision was informed.
    """

    model_config = ConfigDict(extra="forbid")

    approval_id: NonEmptyStr
    run_id: NonEmptyStr
    case_id: NonEmptyStr
    status: ApprovalStatus = ApprovalStatus.PENDING
    requested_outcome: Outcome
    presented_amount: MoneyAmount
    presented_currency: str
    presented_vendor: NonEmptyStr
    presented_exception_categories: list[ExceptionCategory] = Field(default_factory=list)
    presented_citations: list[Citation] = Field(default_factory=list)
    #: Minimum role from the FIN-POL-003 §2 matrix, resolved when the request was created.
    required_role_minimum: str = ""
    #: The FIN-POL-003 §3 conditions that made this a higher-risk transaction. Stored rather
    #: than recomputed at approval time, because the conditions are evaluated against vendor
    #: data as it stood when the case was assessed, and that data can change afterwards.
    higher_risk_reasons: list[str] = Field(default_factory=list)
    requires_second_approval: bool = False
    created_at: datetime
    decided_at: datetime | None = None
    decided_by: str | None = None
    decided_by_role: str | None = None
    decision_comment: str = ""


class DecisionReceipt(BaseModel):
    """The result of recording a consequential outcome.

    ``replayed`` distinguishes the first effective call from a duplicate delivery. Both
    return the same body, which is what makes the callback safe to retry; the flag is how a
    caller can tell, and how the audit log shows that no second posting occurred.
    """

    model_config = ConfigDict(extra="forbid")

    decision_ref: NonEmptyStr
    run_id: NonEmptyStr
    case_id: NonEmptyStr
    outcome: Outcome
    amount: MoneyAmount
    currency: str
    idempotency_key: NonEmptyStr
    recorded_at: datetime
    simulated: bool = True
    replayed: bool = False
    posting_system: str = "SIMULATED_ERP"


class RunView(BaseModel):
    """What ``GET /runs/{id}`` returns.

    Deliberately includes the full event log. The brief asks for a run to be explainable and
    reproducible, and an explanation that requires a second call to a different endpoint is
    one the reviewer has to assemble themselves.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: NonEmptyStr
    case_id: NonEmptyStr
    status: RunStatus
    phase: RunPhase
    created_at: datetime
    updated_at: datetime
    steps_used: int
    tool_calls_used: int
    recommendation: Recommendation | None = None
    result: FinalResult | None = None
    pending_approval: ApprovalRequest | None = None
    decision: DecisionReceipt | None = None
    failure_reason: FailureReason | None = None
    events: list[dict[str, object]] = Field(default_factory=list)
