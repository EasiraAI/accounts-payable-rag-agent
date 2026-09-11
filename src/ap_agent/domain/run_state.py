"""The run state.

One typed object holds everything a run has established. It is serialised to the database
after every phase, which gives three properties the brief asks for at once:

**Resume.** A process that dies mid-run leaves a complete state row. A new process loads it
and re-enters the driver at the stored phase, with all evidence intact. Nothing is held only
in memory.

**Reproducibility.** The state is the run's evidence. An auditor can read it and re-derive
the recommendation by hand, because the rule engine is pure and every input it consumed is
here.

**Explainability.** Facts, inferences and unknowns accumulate separately as the run
progresses, so the final result does not have to reconstruct after the fact which statements
were sourced and which were reasoned.

Accumulators are appended to, never replaced. A phase that runs twice after a resume would
otherwise silently discard what the first attempt established, and the duplicate-suppression
helpers below are what make a repeated phase idempotent in its effect on state.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field

from ap_agent.domain.enums import FailureReason, RunPhase, RunStatus
from ap_agent.domain.evidence import (
    DelegationRecord,
    InvoiceHistoryMatch,
    PurchaseOrder,
    RetrievedChunk,
    VendorRecord,
)
from ap_agent.domain.request import ProcessingRequest
from ap_agent.domain.results import (
    ActionRecord,
    Calculation,
    DecisionReceipt,
    ExceptionRecord,
    Inference,
    PolicyFinding,
    Recommendation,
    SourcedFact,
    Unknown,
)
from ap_agent.domain.rules.fraud import FraudIndicator


def new_run_id() -> str:
    """Opaque run identifier.

    A UUID rather than a sequence: run identifiers appear in logs and URLs, and a
    monotonically increasing integer would leak volume and let one caller enumerate another
    caller's runs.
    """
    return f"run_{uuid.uuid4().hex[:16]}"


def new_approval_id() -> str:
    return f"apr_{uuid.uuid4().hex[:16]}"


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


class RunState(BaseModel):
    """Everything a run has established, serialised after every phase."""

    model_config = ConfigDict(extra="forbid")

    # ---- identity and control ---------------------------------------------------------
    run_id: str
    case_id: str
    correlation_id: str
    status: RunStatus = RunStatus.RUNNING
    phase: RunPhase = RunPhase.INTAKE
    #: Incremented on every persisted write. Used for optimistic concurrency so two workers
    #: cannot both advance the same run.
    version: int = 0
    created_at: datetime
    updated_at: datetime

    # ---- budgets ---------------------------------------------------------------------
    steps_used: int = 0
    tool_calls_used: int = 0
    #: Phases already completed. Consulted on resume so a repeated phase can be skipped
    #: rather than re-executed, which matters for any phase that is not free to repeat.
    completed_phases: list[RunPhase] = Field(default_factory=list)

    # ---- input ------------------------------------------------------------------------
    request: ProcessingRequest

    # ---- gathered evidence ------------------------------------------------------------
    policy_chunks: list[RetrievedChunk] = Field(default_factory=list)
    evidence_chunks: list[RetrievedChunk] = Field(default_factory=list)
    vendor: VendorRecord | None = None
    purchase_order: PurchaseOrder | None = None
    invoice_history: list[InvoiceHistoryMatch] = Field(default_factory=list)
    delegation: DelegationRecord | None = None

    # ---- accumulated conclusions ------------------------------------------------------
    sourced_facts: list[SourcedFact] = Field(default_factory=list)
    inferences: list[Inference] = Field(default_factory=list)
    unknowns: list[Unknown] = Field(default_factory=list)
    policy_findings: list[PolicyFinding] = Field(default_factory=list)
    calculations: list[Calculation] = Field(default_factory=list)
    exceptions: list[ExceptionRecord] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    fraud_indicators: list[FraudIndicator] = Field(default_factory=list)
    invalid_reasons: list[str] = Field(default_factory=list)
    #: Minimum approver role from the FIN-POL-003 §2 matrix, determined during reconciliation.
    required_role_minimum: str = ""
    #: The FIN-POL-003 §3 conditions that make this a higher-risk transaction. Captured when
    #: the case is assessed and carried onto the approval record, so an approver's authority
    #: is later judged against the facts they were shown rather than against vendor data as
    #: it stands at approval time.
    higher_risk_reasons: list[str] = Field(default_factory=list)

    # ---- outcome ----------------------------------------------------------------------
    recommendation: Recommendation | None = None
    approval_id: str | None = None
    decision: DecisionReceipt | None = None
    actions_taken: list[ActionRecord] = Field(default_factory=list)
    failure_reason: FailureReason | None = None
    failure_detail: str = ""

    # ---- construction -----------------------------------------------------------------

    @classmethod
    def create(cls, request: ProcessingRequest, *, now: datetime | None = None) -> Self:
        moment = now or utc_now()
        run_id = new_run_id()
        return cls(
            run_id=run_id,
            case_id=request.case_id,
            # The correlation identifier is separate from the run identifier so that a
            # future caller-supplied trace identifier can be threaded through without
            # changing the run's own identity.
            correlation_id=run_id,
            request=request,
            created_at=moment,
            updated_at=moment,
        )

    # ---- accumulation -----------------------------------------------------------------

    def add_assumptions(self, values: list[str]) -> None:
        """Append assumptions, skipping ones already recorded."""
        for value in values:
            if value not in self.assumptions:
                self.assumptions.append(value)

    def add_unknowns(self, values: list[Unknown]) -> None:
        existing = {(unknown.item, unknown.reason) for unknown in self.unknowns}
        for value in values:
            key = (value.item, value.reason)
            if key not in existing:
                existing.add(key)
                self.unknowns.append(value)

    def add_exceptions(self, values: list[ExceptionRecord]) -> None:
        existing = {(record.category, record.failed_rule) for record in self.exceptions}
        for value in values:
            key = (value.category, value.failed_rule)
            if key not in existing:
                existing.add(key)
                self.exceptions.append(value)

    def add_findings(self, values: list[PolicyFinding]) -> None:
        existing = {(finding.rule, finding.satisfied) for finding in self.policy_findings}
        for value in values:
            key = (value.rule, value.satisfied)
            if key not in existing:
                existing.add(key)
                self.policy_findings.append(value)

    def add_calculations(self, values: list[Calculation]) -> None:
        existing = {calculation.name for calculation in self.calculations}
        for value in values:
            if value.name not in existing:
                existing.add(value.name)
                self.calculations.append(value)

    def add_facts(self, values: list[SourcedFact]) -> None:
        existing = {fact.statement for fact in self.sourced_facts}
        for value in values:
            if value.statement not in existing:
                existing.add(value.statement)
                self.sourced_facts.append(value)

    def add_inferences(self, values: list[Inference]) -> None:
        existing = {inference.statement for inference in self.inferences}
        for value in values:
            if value.statement not in existing:
                existing.add(value.statement)
                self.inferences.append(value)

    def add_indicators(self, values: list[FraudIndicator]) -> None:
        existing = {indicator.code for indicator in self.fraud_indicators}
        for value in values:
            if value.code not in existing:
                existing.add(value.code)
                self.fraud_indicators.append(value)

    def mark_phase_complete(self, phase: RunPhase) -> None:
        if phase not in self.completed_phases:
            self.completed_phases.append(phase)

    # ---- derived views ----------------------------------------------------------------

    @property
    def all_chunks(self) -> list[RetrievedChunk]:
        """Policy and evidence chunks together, deduplicated by chunk identifier."""
        seen: set[str] = set()
        combined: list[RetrievedChunk] = []
        for chunk in [*self.policy_chunks, *self.evidence_chunks]:
            if chunk.chunk_id not in seen:
                seen.add(chunk.chunk_id)
                combined.append(chunk)
        return combined

    @property
    def blocking_exceptions(self) -> list[ExceptionRecord]:
        return [record for record in self.exceptions if record.blocking]

    @property
    def is_terminal(self) -> bool:
        return self.phase.is_terminal

    def touch(self, *, now: datetime | None = None) -> None:
        self.updated_at = now or utc_now()
