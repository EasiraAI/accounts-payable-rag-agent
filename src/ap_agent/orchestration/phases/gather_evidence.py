"""Call the evidence tools the case actually needs, and no others.

Extracted from the orchestrator so that what this phase can reach is visible in its
signature: a clock, a repository, a retriever and a model call, and nothing else.
"""

from __future__ import annotations

import time

from ap_agent.domain.results import (
    Unknown,
)
from ap_agent.domain.run_state import RunState
from ap_agent.observability.events import EventEmitter
from ap_agent.orchestration.phase_context import PhaseContext
from ap_agent.orchestration.phases import (
    EVIDENCE_QUERY,
)
from ap_agent.rag.retriever import EVIDENCE_DOC_TYPES
from ap_agent.tools.base import ToolRunner
from ap_agent.tools.contracts import (
    CHECK_INVOICE_HISTORY,
    GET_PURCHASE_ORDER,
    GET_VENDOR_RECORD,
    RETRIEVE_DOCUMENTS,
    CheckInvoiceHistoryInput,
    CheckInvoiceHistoryOutput,
    GetPurchaseOrderInput,
    GetPurchaseOrderOutput,
    GetVendorInput,
    GetVendorOutput,
    RetrieveDocumentsInput,
    RetrieveDocumentsOutput,
    check_invoice_history,
    get_purchase_order,
    get_vendor_record,
    retrieve_finance_documents,
)
from ap_agent.tools.mock_backends import MockBackends


def run(
    ctx: PhaseContext,
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
                    reason=(f"{CHECK_INVOICE_HISTORY.name} failed: {history_result.error_message}"),
                    impact="duplicate detection could not be performed",
                    how_to_resolve="retry once the invoice history service is reachable",
                    source_attempted=CHECK_INVOICE_HISTORY.name,
                )
            ]
        )

    # Evidence retrieval: only when there is supplier-supplied material to look for.
    needs_evidence_search = bool(state.request.untrusted_texts()) or (
        state.vendor is not None and state.vendor.bank_changed_recently(ctx.clock())
    )
    if needs_evidence_search:
        started = time.perf_counter()
        evidence_result = runner.run(
            RETRIEVE_DOCUMENTS,
            retrieve_finance_documents(ctx.retriever),  # type: ignore[arg-type]
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
