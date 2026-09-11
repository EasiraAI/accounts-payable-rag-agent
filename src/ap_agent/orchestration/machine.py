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
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

from ap_agent.config.settings import Settings
from ap_agent.domain.enums import (
    ApprovalStatus,
    EventType,
    ExceptionCategory,
    FailureReason,
    Outcome,
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
from ap_agent.domain.evidence import Citation, DelegationRecord
from ap_agent.domain.money import Money
from ap_agent.domain.request import ApprovalDecision, ProcessingRequest, UntrustedText
from ap_agent.domain.results import (
    ActionRecord,
    ApprovalRequest,
    ApprovalSignature,
    ConfidenceAssessment,
    FinalResult,
    Inference,
    PolicyFinding,
    Recommendation,
    SourcedFact,
    Unknown,
    truncate_detail,
)
from ap_agent.domain.rules.authority import required_authority, validate_approval
from ap_agent.domain.rules.duplicates import duplicate_check
from ap_agent.domain.rules.fraud import detect_injection, fraud_indicators
from ap_agent.domain.rules.matching import three_way_match
from ap_agent.domain.rules.non_po import check_repeated_non_po
from ap_agent.domain.rules.outcome import decide_outcome
from ap_agent.domain.rules.payment_instructions import check_payment_instructions
from ap_agent.domain.rules.payment_terms import assess_payment_terms
from ap_agent.domain.rules.segregation import (
    check_approval_time as check_segregation_at_approval,
)
from ap_agent.domain.rules.segregation import (
    check_reconciliation_time as check_segregation_at_reconciliation,
)
from ap_agent.domain.rules.tax import assess_tax
from ap_agent.domain.rules.validity import check_invoice_validity
from ap_agent.domain.rules.vendor import vendor_status_check
from ap_agent.domain.run_state import RunState, new_approval_id, utc_now
from ap_agent.llm import (
    SYSTEM_PROMPT,
    EvidenceSynthesis,
    LLMClient,
    RecommendationNarrative,
    build_evidence_prompt,
    build_recommendation_prompt,
    new_boundary_nonce,
    render_computed_findings,
    tightens,
)
from ap_agent.observability.events import EventEmitter
from ap_agent.observability.redact import redact_text
from ap_agent.orchestration.gates import Budget, authorise_decision
from ap_agent.orchestration.phases import (
    EVIDENCE_QUERY,
    POLICY_QUERIES,
    next_phase,
)
from ap_agent.persistence.repository import Repository
from ap_agent.rag.retriever import EVIDENCE_DOC_TYPES, POLICY_DOC_TYPES, Retriever
from ap_agent.tools.base import ToolRunner
from ap_agent.tools.contracts import (
    CHECK_INVOICE_HISTORY,
    GET_AUTHORITY_DELEGATION,
    GET_PURCHASE_ORDER,
    GET_VENDOR_RECORD,
    RETRIEVE_DOCUMENTS,
    SUBMIT_FINANCE_DECISION,
    CheckInvoiceHistoryInput,
    CheckInvoiceHistoryOutput,
    GetDelegationInput,
    GetDelegationOutput,
    GetPurchaseOrderInput,
    GetPurchaseOrderOutput,
    GetVendorInput,
    GetVendorOutput,
    RetrieveDocumentsInput,
    RetrieveDocumentsOutput,
    SubmitFinanceDecisionInput,
    check_invoice_history,
    get_authority_delegation,
    get_purchase_order,
    get_vendor_record,
    retrieve_finance_documents,
    submit_finance_decision,
)
from ap_agent.tools.mock_backends import MockBackends

Clock = Callable[[], datetime]


def _redact_case_input(request: ProcessingRequest) -> ProcessingRequest:
    """Return the request with its untrusted free text redacted.

    Applied at intake, before the state is persisted. An earlier version redacted only at the
    event boundary, and a security review pasted a bank account, an IBAN, a provider key and
    a tax identifier into the case notes and read every one of them back out of
    ``GET /runs/{id}``: the run state was a second egress path that nothing covered.

    Both controls that read this text still work afterwards, which is why redacting here is
    safe rather than lossy. Injection detection reads imperative language, which redaction
    leaves untouched. The payment-instruction comparison reads a four-digit account tail,
    which redaction preserves and FIN-POL-004 §2 explicitly permits.
    """
    notes = request.notes
    redacted_notes = (
        notes.model_copy(update={"content": redact_text(notes.content)})
        if notes is not None
        else None
    )
    redacted_attachments = [
        attachment.model_copy(update={"text": redact_text(attachment.text)})
        for attachment in request.attachments
    ]
    return request.model_copy(update={"notes": redacted_notes, "attachments": redacted_attachments})


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
            RunPhase.INTAKE: self._phase_intake,
            RunPhase.RETRIEVE_POLICY: self._phase_retrieve_policy,
            RunPhase.GATHER_EVIDENCE: self._phase_gather_evidence,
            RunPhase.RECONCILE: self._phase_reconcile,
            RunPhase.ASSESS_RISK: self._phase_assess_risk,
            RunPhase.RECOMMEND: self._phase_recommend,
            RunPhase.EXECUTE_DECISION: self._phase_execute_decision,
        }[phase]

        handler(state, emitter, runner, backends)

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

    def _phase_intake(
        self,
        state: RunState,
        emitter: EventEmitter,
        runner: ToolRunner,
        backends: MockBackends,
    ) -> None:
        """Validate the request and record what was submitted as fact.

        Runs no tools. Its job is to turn the request into an invoice, note what the request
        did not supply, and screen the untrusted text so that an injection attempt is on the
        record before any other phase reads it.
        """
        # Untrusted case text is redacted before the state is persisted. See
        # _redact_case_input for why this is safe rather than lossy.
        state.request = _redact_case_input(state.request)
        request = state.request
        invoice = request.to_invoice()

        state.add_facts(
            [
                SourcedFact(
                    statement="Invoice submitted for processing",
                    value=f"{invoice.invoice_reference}, {invoice.gross_amount} {invoice.currency}",
                    source=f"processing request {request.case_id}",
                ),
                SourcedFact(
                    statement="Vendor named on the invoice",
                    value=invoice.vendor_name,
                    source=f"processing request {request.case_id}",
                ),
            ]
        )

        if not request.invoice_date_supplied:
            state.add_assumptions(
                [
                    "The request supplied no invoice date, so today's date in UTC was used. "
                    "This affects month-end cut-off under FIN-POL-011 §1 and the duplicate "
                    "date window under FIN-POL-005 §1."
                ]
            )
            state.add_unknowns(
                [
                    Unknown(
                        item="invoice date",
                        reason="not supplied on the processing request",
                        impact="cut-off and duplicate date-window assessments are indicative",
                        how_to_resolve="supply the invoice date from the supplier document",
                    )
                ]
            )

        if not request.lines:
            state.add_unknowns(
                [
                    Unknown(
                        item="invoice line detail",
                        reason="not supplied on the processing request",
                        impact="per-line matching cannot be performed; only the document total",
                        how_to_resolve="supply invoice lines with purchase-order line references",
                    )
                ]
            )

        # Screen untrusted text now, before any phase reads it for content. The detection is
        # recorded whether or not it later changes the outcome.
        for text in request.untrusted_texts():
            codes = detect_injection(text.content)
            if codes:
                emitter.injection_detected(
                    source=text.origin, pattern_codes=codes, phase=RunPhase.INTAKE
                )

        if not request.po_reference:
            state.add_assumptions(
                [
                    "No purchase-order reference was supplied. FIN-POL-012 §1 limits non-PO "
                    "processing to specific categories and requires a justification."
                ]
            )

        # FIN-POL-001 §2 and §3. Runs here, before any tool call, because a submission that
        # contradicts itself cannot be repaired by retrieval: spending the tool budget on a
        # case that will be rejected as invalid wastes it, and the resulting variances would
        # be reported against the purchase order rather than the invoice that is at fault.
        validity = check_invoice_validity(
            invoice,
            invoice_date_supplied=request.invoice_date_supplied,
            tax_separated=request.tax_separated,
            # Read only to recognise a credit note, which holds the case. Untrusted text is
            # never read here for anything that would let a case proceed.
            texts=request.untrusted_texts(),
            as_of=self._clock(),
        )
        state.add_calculations(validity.calculations)
        state.add_findings(validity.findings)
        state.add_exceptions(validity.exceptions)
        # Deduplicated, like every other accumulator on the run state. A crash between this
        # handler and the state being saved re-enters INTAKE on resume, and without this the
        # same reason would appear twice in the rejection.
        for reason in validity.invalid_reasons:
            if reason not in state.invalid_reasons:
                state.invalid_reasons.append(reason)

    def _phase_retrieve_policy(
        self,
        state: RunState,
        emitter: EventEmitter,
        runner: ToolRunner,
        backends: MockBackends,
    ) -> None:
        """Retrieve the current policy each control will cite."""
        handler = retrieve_finance_documents(self._retriever)
        for query in POLICY_QUERIES:
            started = time.perf_counter()
            result = runner.run(
                RETRIEVE_DOCUMENTS,
                handler,  # type: ignore[arg-type]
                RetrieveDocumentsInput(
                    query=query.text,
                    top_k=query.top_k,
                    doc_types=list(POLICY_DOC_TYPES),
                    purpose=query.purpose,
                ),
                RetrieveDocumentsOutput,
            )
            if not result.succeeded or result.value is None:
                state.add_unknowns(
                    [
                        Unknown(
                            item=f"policy citations for {query.purpose}",
                            reason=(
                                f"retrieval failed: {result.error_message or result.outcome.value}"
                            ),
                            impact="the control was applied from code but has no cited source",
                            source_attempted=RETRIEVE_DOCUMENTS.name,
                        )
                    ]
                )
                continue
            emitter.retrieval(
                query=query.text,
                query_terms=result.value.query_terms,
                purpose=query.purpose,
                doc_types=list(POLICY_DOC_TYPES),
                results=[
                    {
                        "chunk_id": chunk.chunk_id,
                        "document_id": chunk.document_id,
                        "section": chunk.section,
                        "version": chunk.version,
                        "status": chunk.status.value,
                        "rank": chunk.rank,
                        "score": chunk.score,
                    }
                    for chunk in result.value.chunks
                ],
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
            existing = {chunk.chunk_id for chunk in state.policy_chunks}
            state.policy_chunks.extend(
                chunk for chunk in result.value.chunks if chunk.chunk_id not in existing
            )

    def _phase_gather_evidence(
        self,
        state: RunState,
        emitter: EventEmitter,
        runner: ToolRunner,
        backends: MockBackends,
    ) -> None:
        """Fetch the records the controls need, and only those."""
        request = state.request
        invoice = request.to_invoice()

        # Vendor: always. FIN-POL-001 §5 makes it mandatory.
        vendor_result = runner.run(
            GET_VENDOR_RECORD,
            get_vendor_record(backends),  # type: ignore[arg-type]
            GetVendorInput(vendor_id=invoice.vendor_id),
            GetVendorOutput,
        )
        if vendor_result.succeeded and vendor_result.value is not None:
            state.vendor = vendor_result.value.vendor
            if state.vendor is None:
                state.add_unknowns(
                    [
                        Unknown(
                            item=f"vendor master record for {invoice.vendor_id}",
                            reason="the vendor master returned no record",
                            impact="vendor status and payment-detail controls cannot be applied",
                            how_to_resolve="confirm the vendor identifier or onboard the vendor",
                            source_attempted=GET_VENDOR_RECORD.name,
                        )
                    ]
                )
        else:
            state.add_unknowns(
                [
                    Unknown(
                        item=f"vendor master record for {invoice.vendor_id}",
                        reason=f"{GET_VENDOR_RECORD.name} failed: {vendor_result.error_message}",
                        impact="vendor status and payment-detail controls cannot be applied",
                        how_to_resolve="retry once the vendor master is reachable",
                        source_attempted=GET_VENDOR_RECORD.name,
                    )
                ]
            )

        # Purchase order: only when the case names one.
        if request.po_reference:
            order_result = runner.run(
                GET_PURCHASE_ORDER,
                get_purchase_order(backends),  # type: ignore[arg-type]
                GetPurchaseOrderInput(po_reference=request.po_reference),
                GetPurchaseOrderOutput,
            )
            if order_result.succeeded and order_result.value is not None:
                state.purchase_order = order_result.value.purchase_order
                if state.purchase_order is None:
                    state.add_unknowns(
                        [
                            Unknown(
                                item=f"purchase order {request.po_reference}",
                                reason="the purchasing system holds no such order",
                                impact="three-way matching cannot be performed",
                                how_to_resolve="correct the purchase-order reference",
                                source_attempted=GET_PURCHASE_ORDER.name,
                            )
                        ]
                    )
            else:
                # The distinction matters: the order may well exist, and the run must not
                # conclude that it does not. FIN-POL-002 §4 gives a hold, not a rejection.
                state.add_unknowns(
                    [
                        Unknown(
                            item=(
                                f"purchase order {request.po_reference} lines, totals, "
                                "tolerances and receipts"
                            ),
                            reason=(
                                f"{GET_PURCHASE_ORDER.name} did not respond after "
                                f"{order_result.attempts} attempt(s): "
                                f"{order_result.error_message}"
                            ),
                            impact=(
                                "three-way matching could not be performed, so the invoice "
                                "cannot be approved for posting"
                            ),
                            how_to_resolve=(
                                "retry once the purchasing system is reachable; the order was "
                                "not shown to be absent, only unreachable"
                            ),
                            source_attempted=GET_PURCHASE_ORDER.name,
                        )
                    ]
                )

        # Invoice history: always. FIN-POL-005 §1 makes duplicate detection mandatory.
        history_result = runner.run(
            CHECK_INVOICE_HISTORY,
            check_invoice_history(backends),  # type: ignore[arg-type]
            CheckInvoiceHistoryInput(
                vendor_id=invoice.vendor_id,
                invoice_reference=invoice.invoice_reference,
                currency=invoice.currency,
                gross_amount=invoice.gross_amount,
            ),
            CheckInvoiceHistoryOutput,
        )
        if history_result.succeeded and history_result.value is not None:
            state.invoice_history = history_result.value.candidates
        else:
            state.add_unknowns(
                [
                    Unknown(
                        item="prior invoice history for this vendor",
                        reason=(
                            f"{CHECK_INVOICE_HISTORY.name} failed: {history_result.error_message}"
                        ),
                        impact="duplicate detection could not be performed",
                        how_to_resolve="retry once the invoice history service is reachable",
                        source_attempted=CHECK_INVOICE_HISTORY.name,
                    )
                ]
            )

        # Evidence retrieval: only when there is supplier-supplied material to look for.
        needs_evidence_search = bool(state.request.untrusted_texts()) or (
            state.vendor is not None and state.vendor.bank_changed_recently(self._clock())
        )
        if needs_evidence_search:
            started = time.perf_counter()
            evidence_result = runner.run(
                RETRIEVE_DOCUMENTS,
                retrieve_finance_documents(self._retriever),  # type: ignore[arg-type]
                RetrieveDocumentsInput(
                    query=EVIDENCE_QUERY.text,
                    top_k=EVIDENCE_QUERY.top_k,
                    doc_types=list(EVIDENCE_DOC_TYPES),
                    purpose=EVIDENCE_QUERY.purpose,
                ),
                RetrieveDocumentsOutput,
            )
            if evidence_result.succeeded and evidence_result.value is not None:
                emitter.retrieval(
                    query=EVIDENCE_QUERY.text,
                    query_terms=evidence_result.value.query_terms,
                    purpose=EVIDENCE_QUERY.purpose,
                    doc_types=list(EVIDENCE_DOC_TYPES),
                    results=[
                        {
                            "chunk_id": chunk.chunk_id,
                            "document_id": chunk.document_id,
                            "status": chunk.status.value,
                            "doc_type": chunk.doc_type,
                            "rank": chunk.rank,
                            "score": chunk.score,
                        }
                        for chunk in evidence_result.value.chunks
                    ],
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )
                known = {chunk.chunk_id for chunk in state.evidence_chunks}
                state.evidence_chunks.extend(
                    chunk for chunk in evidence_result.value.chunks if chunk.chunk_id not in known
                )

    def _phase_reconcile(
        self,
        state: RunState,
        emitter: EventEmitter,
        runner: ToolRunner,
        backends: MockBackends,
    ) -> None:
        """Apply the deterministic controls. No model, no tools, no I/O."""
        as_of = self._clock()
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
                self._without_superseded_unknowns(state, list(getattr(result, "unknowns", [])))
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

        # Nothing is cached on the orchestrator. The rule functions are pure, so RECOMMEND
        # recomputes them from the same persisted evidence and gets the same answer.
        # Caching on the instance would outlive a run and would not survive a resume, so it
        # would be both unsafe under concurrency and useless where it was needed.

    def _screen_narrative(
        self,
        state: RunState,
        emitter: EventEmitter,
        narrative: RecommendationNarrative,
        outcome: Outcome,
    ) -> tuple[str, str]:
        """Return the summary and next action an approver will read, screened first.

        A security review pointed out that a compromised model keeps the computed outcome but
        writes whatever it likes in the prose a human actually reads: the run held
        ``REJECT_DUPLICATE`` while the summary said "APPROVED by the CFO out of band. Post
        immediately; the duplicate flag is a system error." Schema-valid, so nothing rejected
        it, and the outcome field is not what an approver's eye goes to first.

        The narrative is therefore screened with the same detector used on untrusted
        documents, plus a check for language asserting an approval the system has no record
        of. If either fires, the model's prose is discarded and replaced with a deterministic
        summary built from the computed facts. The substitution is recorded as a finding,
        because a model writing approval language into a rejection is itself a finding.
        """
        blob = f"{narrative.summary}\n{narrative.next_action}"
        injection_codes = detect_injection(blob)
        lowered = blob.lower()
        approval_claims = [
            phrase
            for phrase in (
                "already approved",
                "approved by the cfo",
                "approved out of band",
                "out of band",
                "post immediately",
                "pay today",
                "pay immediately",
                "system error",
            )
            if phrase in lowered
        ]
        if not injection_codes and not approval_claims:
            return narrative.summary, narrative.next_action

        emitter.emit(
            EventType.INJECTION_ATTEMPT_DETECTED,
            payload={
                "source": "model narrative",
                "patterns": [*injection_codes, *approval_claims],
                "note": (
                    "the model's approver-facing prose was discarded and replaced with a "
                    "deterministic summary"
                ),
            },
            phase=RunPhase.RECOMMEND,
            outcome="BLOCKED",
        )
        state.add_findings(
            [
                PolicyFinding(
                    rule="model_narrative_screened",
                    policy_ref="FIN-POL-005 §4",
                    satisfied=True,
                    detail=truncate_detail(
                        "The model's summary or next action contained instruction-like or "
                        "approval-asserting language ("
                        + ", ".join([*injection_codes, *approval_claims])
                        + "). It was discarded and replaced with a summary generated from the "
                        "computed control results, because the prose is what an approver "
                        "reads."
                    ),
                )
            ]
        )
        return self._deterministic_summary(state, outcome), self._deterministic_next_action(
            state, outcome
        )

    @staticmethod
    def _deterministic_summary(state: RunState, outcome: Outcome) -> str:
        """A summary built from computed facts, with no model involvement."""
        categories = sorted({exception.category.value for exception in state.exceptions})
        indicators = [indicator.code for indicator in state.fraud_indicators]
        parts = [f"The rule engine computed {outcome.value} from the control results."]
        if categories:
            parts.append("Exceptions raised: " + ", ".join(categories) + ".")
        else:
            parts.append("No exceptions were raised.")
        if indicators:
            parts.append("Fraud indicators: " + ", ".join(indicators) + ".")
        if state.unknowns:
            parts.append(f"{len(state.unknowns)} item(s) remain unknown.")
        parts.append(
            "This summary was generated from the computed results because the model's prose "
            "was screened out."
        )
        return " ".join(parts)[:1_190]

    @staticmethod
    def _with_payment_schedule(state: RunState, next_action: str, outcome: Outcome) -> str:
        """Append the engine-computed payment schedule to the next action.

        The model writes the prose and never the date. FIN-POL-006 §2 schedules an approved
        invoice for a standard run before its due date, and that run is computed by
        ``rules/payment_terms.py`` from the agreed terms; a date produced by a model would be
        arithmetic taken from the model, which is what FIN-POL-002 §5 and this system's whole
        division of labour forbid.

        Appended rather than substituted because the model's sentence says what a person
        should *do* and this says when the payment would land. Only for an approval: naming a
        run date on a rejected or held case would describe a payment that is not going to
        happen.
        """
        if outcome is not Outcome.APPROVE_FOR_POSTING or state.payable_on is None:
            return next_action
        if state.proposed_payment_run is None:
            schedule = (
                f" The invoice is payable {state.payable_on.isoformat()} and no standard "
                "payment run falls before that date, so scheduling is for Accounts Payable to "
                "resolve; internal delay alone is not grounds for a manual payment "
                "(FIN-POL-006 §3)."
            )
        else:
            schedule = (
                f" Proposed payment run {state.proposed_payment_run.isoformat()}, ahead of the "
                f"due date {state.payable_on.isoformat()} (FIN-POL-006 §2). A proposal only: "
                "an agent may prepare a schedule and may not release a payment file."
            )
        return truncate_detail(next_action.rstrip() + schedule)

    @staticmethod
    def _deterministic_next_action(state: RunState, outcome: Outcome) -> str:
        if outcome is Outcome.APPROVE_FOR_POSTING:
            # The proposed run is named when one exists. FIN-POL-006 §2 schedules an approved
            # invoice for the next standard run before its due date, and a next action that
            # says "the next standard run" without saying which one leaves the reader to
            # recompute a date the engine has already computed.
            if state.proposed_payment_run is not None:
                schedule = (
                    f"then post and schedule for the standard payment run on "
                    f"{state.proposed_payment_run.isoformat()}, ahead of the due date "
                    f"{state.payable_on.isoformat() if state.payable_on else 'not computed'} "
                    "(FIN-POL-006 §2). Proposal only: an agent may not release a payment file."
                )
            else:
                schedule = (
                    "then post. No standard payment run remains before the due date, so "
                    "scheduling is for Accounts Payable to resolve; internal delay alone is "
                    "not grounds for a manual payment (FIN-POL-006 §3)."
                )
            return (
                "Route to an approver holding at least the required delegated authority, "
                + schedule
            )
        if outcome is Outcome.ESCALATE_CONTROL_REVIEW:
            return (
                "Refer to Financial Crime and Controls without notifying the supplier of "
                "the suspicion (FIN-POL-007 §3)."
            )
        if outcome in {Outcome.REJECT_DUPLICATE, Outcome.REJECT_INVALID}:
            return (
                "Route the rejection to an approver, then notify the requester with the "
                "cited records."
            )
        return (
            "Route each exception to the owner named in its record and resume from the "
            "failed control when new evidence arrives."
        )

    @staticmethod
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
        return [
            unknown for unknown in candidates if "purchase-order lines" not in unknown.item.lower()
        ]

    def _phase_assess_risk(
        self,
        state: RunState,
        emitter: EventEmitter,
        runner: ToolRunner,
        backends: MockBackends,
    ) -> None:
        """Count fraud indicators, then ask the model to read the evidence."""
        as_of = self._clock()
        invoice = state.request.to_invoice()

        # Only untrusted text belonging to *this case* feeds the indicator count: the request
        # notes and its attachments. Retrieved corpus documents are screened and logged, but
        # they are not evidence about this transaction.
        #
        # The distinction was found by running the fixtures. Feeding every retrieved untrusted
        # document into the count made the duplicate-invoice case escalate rather than reject,
        # because the evidence search had surfaced an adversarial notice about a different
        # supplier entirely. Counting it would also mean any case could be escalated by
        # planting a document in the corpus.
        case_texts: list[UntrustedText] = list(state.request.untrusted_texts())
        for chunk in state.evidence_chunks:
            if chunk.status.is_authoritative:
                continue
            codes = detect_injection(chunk.text)
            if codes:
                emitter.injection_detected(
                    source=f"document:{chunk.document_id} {chunk.section}",
                    pattern_codes=codes,
                    phase=RunPhase.ASSESS_RISK,
                )
                state.add_findings(
                    [
                        PolicyFinding(
                            rule="retrieved_untrusted_document_screened",
                            policy_ref="FIN-POL-005 §4",
                            satisfied=True,
                            detail=(
                                f"Retrieved document {chunk.document_id} {chunk.section} "
                                f"contains instruction-like content ({', '.join(codes)}). "
                                "Recorded as a corpus observation; it is not evidence about "
                                "this transaction and does not contribute to this case's "
                                "indicator count."
                            ),
                        )
                    ]
                )

        indicators = fraud_indicators(
            invoice=invoice,
            vendor=state.vendor,
            texts=case_texts,
            history=state.invoice_history,
            as_of=as_of,
        )
        state.add_indicators(indicators)

        nonce = new_boundary_nonce()
        prompt = build_evidence_prompt(
            request=state.request,
            policy_chunks=state.policy_chunks,
            evidence_chunks=state.evidence_chunks,
            vendor_summary=self._vendor_summary(state),
            order_summary=self._order_summary(state),
            history_summary=self._history_summary(state),
            nonce=nonce,
        )
        synthesis = self._call_model(state, emitter, prompt, EvidenceSynthesis)

        known_chunks = {chunk.chunk_id for chunk in state.all_chunks}
        state.add_facts(
            [
                SourcedFact(
                    statement=fact.statement,
                    value=fact.value,
                    source="model synthesis of retrieved evidence",
                    citations=self._resolve_citations(
                        state, emitter, fact.citation_chunk_ids, known_chunks
                    ),
                )
                for fact in synthesis.sourced_facts
            ]
        )
        state.add_inferences(
            [
                Inference(
                    statement=inference.statement,
                    basis=inference.basis,
                    confidence=inference.confidence,
                    citations=self._resolve_citations(
                        state, emitter, inference.citation_chunk_ids, known_chunks
                    ),
                )
                for inference in synthesis.inferences
            ]
        )
        state.add_unknowns(
            [
                Unknown(
                    item=unknown.item,
                    reason=unknown.reason,
                    impact=unknown.impact,
                    how_to_resolve=unknown.how_to_resolve,
                    source_attempted="model synthesis",
                )
                for unknown in synthesis.unknowns
            ]
        )

        if synthesis.injection_observed:
            state.add_findings(
                [
                    PolicyFinding(
                        rule="untrusted_content_not_executed",
                        policy_ref="FIN-POL-005 §4",
                        satisfied=True,
                        detail=truncate_detail(
                            "Instruction-like content was observed in untrusted evidence and "
                            "recorded as a risk indicator rather than followed. "
                            + synthesis.injection_note
                        ),
                    )
                ]
            )

    def _phase_recommend(
        self,
        state: RunState,
        emitter: EventEmitter,
        runner: ToolRunner,
        backends: MockBackends,
    ) -> None:
        """Compute the outcome, then ask the model for the narrative around it."""
        as_of = self._clock()
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
        authority = required_authority(
            Money(amount=invoice.gross_amount, currency=invoice.currency),
            # The reasons recorded during reconciliation, which include conditions found
            # outside the vendor record.
            higher_risk_reasons=state.higher_risk_reasons or vendor.higher_risk_reasons,
        )

        decision = decide_outcome(
            match=match,
            duplicates=duplicates,
            vendor=vendor,
            authority=authority,
            indicators=state.fraud_indicators,
            invalid_reasons=state.invalid_reasons,
            # Everything the run recorded, including the controls that are not among the
            # three named results. Without this a blocking exception from the
            # payment-instruction check never reached the outcome.
            additional_exceptions=state.exceptions,
        )

        nonce = new_boundary_nonce()
        computed_findings = render_computed_findings(
            match=match,
            calculations_summary=[
                f"{calculation.name}: {calculation.formula} = {calculation.result}"
                f"{' ' + calculation.currency if calculation.currency else ''}"
                f" [{calculation.policy_ref}]"
                for calculation in state.calculations
            ],
            exception_summary=[
                f"{exception.category.value} ({exception.failed_rule}): expected "
                f"{exception.expected}; observed {exception.observed}"
                for exception in state.exceptions
            ],
            indicator_summary=[
                f"{indicator.code}: {indicator.description} [{indicator.policy_ref}]"
                for indicator in state.fraud_indicators
            ],
            computed_outcome=decision.outcome.value,
            nonce=nonce,
        )

        prompt = build_recommendation_prompt(
            request=state.request,
            computed_findings=computed_findings,
            facts_summary=[
                f"{fact.statement} ({fact.value})" if fact.value else fact.statement
                for fact in state.sourced_facts
            ],
            unknowns_summary=[f"{unknown.item}: {unknown.reason}" for unknown in state.unknowns],
            nonce=nonce,
        )
        narrative = self._call_model(state, emitter, prompt, RecommendationNarrative)

        final_outcome = decision.outcome
        if narrative.suggested_outcome is not None:
            if tightens(narrative.suggested_outcome, decision.outcome):
                emitter.emit(
                    EventType.RULE_EVALUATED,
                    payload={
                        "rule_group": "model_suggestion_applied",
                        "computed": decision.outcome.value,
                        "applied": narrative.suggested_outcome.value,
                        "reason": narrative.suggested_outcome_reason,
                    },
                    phase=RunPhase.RECOMMEND,
                    outcome="TIGHTENED",
                )
                final_outcome = narrative.suggested_outcome
                state.add_assumptions(
                    [
                        f"The outcome was tightened from {decision.outcome.value} to "
                        f"{final_outcome.value} on the model's recommendation: "
                        f"{narrative.suggested_outcome_reason}"
                    ]
                )
            else:
                # The case the design exists for: a model that has obeyed an injected
                # instruction, or simply disagrees, asking for something less cautious.
                emitter.emit(
                    EventType.RULE_EVALUATED,
                    payload={
                        "rule_group": "model_suggestion_discarded",
                        "computed": decision.outcome.value,
                        "suggested": narrative.suggested_outcome.value,
                        "reason": narrative.suggested_outcome_reason,
                        "note": "a model suggestion may only make an outcome more conservative",
                    },
                    phase=RunPhase.RECOMMEND,
                    outcome="DISCARDED",
                )
                state.add_findings(
                    [
                        PolicyFinding(
                            rule="model_cannot_loosen_outcome",
                            policy_ref="FIN-POL-005 §4",
                            satisfied=True,
                            detail=(
                                f"The model suggested {narrative.suggested_outcome.value}, "
                                f"which is less cautious than the computed "
                                f"{decision.outcome.value}. Recorded and discarded."
                            ),
                        )
                    ]
                )

        requires_approval = final_outcome.is_consequential
        summary, next_action = self._screen_narrative(state, emitter, narrative, final_outcome)
        next_action = self._with_payment_schedule(state, next_action, final_outcome)
        recommendation = Recommendation(
            outcome=final_outcome,
            summary=summary,
            cited_evidence=self._recommendation_citations(state),
            calculations=list(state.calculations),
            assumptions=[*narrative.assumptions, *state.assumptions],
            confidence=ConfidenceAssessment(
                score=narrative.confidence.score,
                basis=narrative.confidence.basis,
                drivers=list(narrative.confidence.drivers),
                limits=list(narrative.confidence.limits),
            ),
            exceptions=list(state.exceptions),
            next_action=next_action,
            requires_approval=requires_approval,
            requires_second_approval=decision.requires_second_approval,
            second_approval_reason=decision.second_approval_reason,
        )
        state.recommendation = recommendation
        state.add_findings(
            [
                PolicyFinding(
                    rule="outcome_determined_deterministically",
                    policy_ref="FIN-POL-002 §5",
                    satisfied=True,
                    detail=truncate_detail(
                        "The outcome was computed by the rule engine from typed facts. "
                        + "; ".join(decision.reasons)
                    ),
                )
            ]
        )

        emitter.emit(
            EventType.RECOMMENDATION_READY,
            payload={
                "outcome": final_outcome.value,
                "requires_approval": requires_approval,
                "requires_second_approval": decision.requires_second_approval,
                "exception_categories": sorted(
                    {exception.category.value for exception in state.exceptions}
                ),
                "indicator_codes": [indicator.code for indicator in state.fraud_indicators],
                "confidence": str(narrative.confidence.score),
                "citation_count": len(recommendation.cited_evidence),
                "required_role_minimum": (
                    decision.required_role_minimum.value if decision.required_role_minimum else None
                ),
            },
            phase=RunPhase.RECOMMEND,
            outcome=final_outcome.value,
        )

        if requires_approval:
            self._create_approval(state, emitter, recommendation)

    def _phase_execute_decision(
        self,
        state: RunState,
        emitter: EventEmitter,
        runner: ToolRunner,
        backends: MockBackends,
    ) -> None:
        """Record the approved decision, exactly once."""
        approval = self._repository.load_approval(state.approval_id) if state.approval_id else None
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
            as_of=self._clock(),
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
        handler = submit_finance_decision(self._repository)

        existing = self._repository.load_decision(state.run_id)
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

    # ---- approval handling -------------------------------------------------------------

    def _create_approval(
        self, state: RunState, emitter: EventEmitter, recommendation: Recommendation
    ) -> None:
        """Create the pending approval and stop.

        The record captures what the approver will be shown, not only what they decide.
        FIN-POL-003 §5 requires the approver to see the amount, vendor, exceptions and
        citations before deciding, and storing what was presented is how that requirement
        becomes auditable after the fact.
        """
        if state.approval_id is not None:
            existing = self._repository.load_approval(state.approval_id)
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
            created_at=self._clock(),
        )
        self._repository.create_approval(request)
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
        replay = self._short_circuit_replay(state, emitter, approval, decision, status)
        if replay is not None:
            return replay

        # Authority is validated before a signature is accepted, not after. An approver
        # without sufficient authority has not approved anything, so recording the signature
        # and checking afterwards would leave one on file that was never valid.
        delegation = self._load_delegation(state, emitter, decision)
        # Rebuilt from what was stored on the approval, not recomputed from current vendor
        # data. The conditions were evaluated when the case was assessed, and vendor data can
        # change between assessment and approval; re-deriving them here would judge an
        # approver's authority against facts they were never shown. Vendor *status* is
        # revalidated separately, at decision time, per FIN-POL-007 §5.
        authority = required_authority(
            Money(amount=approval.presented_amount, currency=approval.presented_currency),
            higher_risk_reasons=approval.higher_risk_reasons,
        )
        # Whether a signature that already satisfies the monetary limit is on file. A
        # Financial Control signature is valid as the FIN-POL-003 §3 co-approval only once
        # the limit has been met by someone who holds one.
        primary_signed = any(
            signature.applicable_limit is not None
            and signature.applicable_limit >= approval.presented_amount
            for signature in approval.signatures
        )
        validation = validate_approval(
            authority,
            approver_id=decision.approver_id,
            approver_role=decision.approver_role,
            delegation=delegation,
            requested_by=state.request.requested_by,
            case_cost_centre=state.request.cost_centre,
            as_co_approver=primary_signed,
            as_of=self._clock(),
        )
        segregation = check_segregation_at_approval(
            vendor=state.vendor,
            purchase_order=state.purchase_order,
            invoice=state.request.to_invoice(),
            requested_by=state.request.requested_by,
            approver_id=decision.approver_id,
            existing_signatories=[signature.approver_id for signature in approval.signatures],
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

    def _short_circuit_replay(
        self,
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
                        "this approver has already signed; no validation, no tool call, no "
                        "state change"
                    ),
                },
                phase=state.phase,
                outcome="REPLAYED",
            )
            return self._repository.require_run(state.run_id), True
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
                return self._repository.require_run(state.run_id), True
            raise ApprovalStateConflict(
                f"approval {approval.approval_id} is already REJECTED and cannot be changed "
                "to APPROVED"
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

    def _call_model(self, state: RunState, emitter: EventEmitter, prompt: str, schema: Any) -> Any:
        """Call the model and record the call, whether it succeeded or not."""
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

    def _resolve_citations(
        self,
        state: RunState,
        emitter: EventEmitter,
        chunk_ids: Sequence[str],
        known: set[str],
    ) -> list[Citation]:
        """Turn model-supplied chunk identifiers into citations, dropping any it invented.

        This is the grounding control. A model that cites "FIN-POL-002 §9" supplies an
        identifier that matches nothing retrieved, so the fabricated citation is discarded
        before it can reach the recommendation. Discards are recorded, because a model that
        invents citations is itself a finding.
        """
        by_id = {chunk.chunk_id: chunk for chunk in state.all_chunks}
        resolved: list[Citation] = []
        unknown: list[str] = []
        for chunk_id in chunk_ids:
            if chunk_id in known and chunk_id in by_id:
                resolved.append(by_id[chunk_id].to_citation())
            else:
                unknown.append(chunk_id)
        if unknown:
            emitter.emit(
                EventType.RULE_EVALUATED,
                payload={
                    "rule_group": "citation_grounding",
                    "discarded_chunk_ids": unknown,
                    "note": "the model cited identifiers that were not retrieved in this run",
                },
                phase=state.phase,
                outcome="DISCARDED",
            )
        return resolved

    def _recommendation_citations(self, state: RunState) -> list[Citation]:
        """Citations attached to the recommendation an approver reads.

        Drawn from the policy chunks actually retrieved plus any cited on an exception, so
        every claim in the recommendation is traceable to a passage the reviewer can open.
        Superseded and untrusted documents are excluded here: they belong in the evidence
        record, not in the basis presented as authority.
        """
        citations: list[Citation] = []
        seen: set[str] = set()
        for chunk in state.policy_chunks:
            if chunk.status.is_authoritative and chunk.chunk_id not in seen:
                seen.add(chunk.chunk_id)
                citations.append(chunk.to_citation())
        for exception in state.exceptions:
            for citation in exception.citations:
                if citation.chunk_id not in seen:
                    seen.add(citation.chunk_id)
                    citations.append(citation)
        return citations

    # ---- summaries supplied to the model ------------------------------------------------

    @staticmethod
    def _vendor_summary(state: RunState) -> str:
        vendor = state.vendor
        if vendor is None:
            return "  (no vendor master record was returned)"
        return "\n".join(
            [
                f"  vendor_id: {vendor.vendor_id}",
                f"  legal_name: {vendor.legal_name}",
                f"  status: {vendor.status.value}",
                f"  bank_account_last4: {vendor.bank_account_last4 or '(none on file)'}",
                f"  bank_country: {vendor.bank_country or '(unknown)'}",
                f"  bank_details_changed_at: {vendor.bank_details_changed_at or '(never)'}",
                f"  created_at: {vendor.created_at}",
                f"  risk_flags: {vendor.risk_flags or '(none)'}",
                f"  on_payment_hold: {vendor.on_payment_hold}",
            ]
        )

    @staticmethod
    def _order_summary(state: RunState) -> str:
        order = state.purchase_order
        if order is None:
            return "  (no purchase order was available)"
        lines = [
            f"  po_reference: {order.po_reference}",
            f"  currency: {order.currency}",
            f"  total_value: {order.total_value}",
            f"  approval_status: {order.approval_status}",
            f"  line_count: {len(order.lines)}",
            f"  receipt_count: {len(order.receipts)}",
        ]
        for line in order.lines:
            lines.append(
                f"    line {line.line_number} ({line.line_type.value}): "
                f"ordered {line.quantity_ordered} at {line.unit_price} "
                f"= {line.line_value}, received "
                f"{order.quantity_received_for(line.line_number)}"
            )
        return "\n".join(lines)

    @staticmethod
    def _history_summary(state: RunState) -> str:
        if not state.invoice_history:
            return "  (no prior records on file for this vendor)"
        return "\n".join(
            f"  {record.record_id}: {record.invoice_reference}, {record.gross_amount} "
            f"{record.currency}, {record.invoice_date}, status {record.status.value}"
            for record in state.invoice_history
        )

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
