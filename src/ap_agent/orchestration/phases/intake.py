"""Validate the submission and record what it says, before any tool is called.

Extracted from the orchestrator so that what this phase can reach is visible in its
signature: a clock, a repository, a retriever and a model call, and nothing else.
"""

from __future__ import annotations

from ap_agent.domain.enums import (
    RunPhase,
)
from ap_agent.domain.results import (
    SourcedFact,
    Unknown,
)
from ap_agent.domain.rules.fraud import detect_injection
from ap_agent.domain.rules.validity import check_invoice_validity
from ap_agent.domain.run_state import RunState
from ap_agent.observability.events import EventEmitter
from ap_agent.orchestration.phase_context import PhaseContext
from ap_agent.orchestration.redaction import redact_case_input
from ap_agent.tools.base import ToolRunner
from ap_agent.tools.mock_backends import MockBackends


def run(
    ctx: PhaseContext,
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
    state.request = redact_case_input(state.request)
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
        as_of=ctx.clock(),
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
