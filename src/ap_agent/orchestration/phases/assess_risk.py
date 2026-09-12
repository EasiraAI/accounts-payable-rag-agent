"""Read untrusted text into structured statements, and count indicators.

Extracted from the orchestrator so that what this phase can reach is visible in its
signature: a clock, a repository, a retriever and a model call, and nothing else.
"""

from __future__ import annotations

from ap_agent.domain.enums import (
    RunPhase,
)
from ap_agent.domain.request import UntrustedText
from ap_agent.domain.results import (
    Inference,
    PolicyFinding,
    SourcedFact,
    Unknown,
    truncate_detail,
)
from ap_agent.domain.rules.fraud import detect_injection, fraud_indicators
from ap_agent.domain.run_state import RunState
from ap_agent.llm import (
    EvidenceSynthesis,
    build_evidence_prompt,
    new_boundary_nonce,
)
from ap_agent.observability.events import EventEmitter
from ap_agent.orchestration.citations import resolve_citations
from ap_agent.orchestration.phase_context import PhaseContext
from ap_agent.orchestration.summaries import (
    history_summary,
    order_summary,
    vendor_summary,
)
from ap_agent.tools.base import ToolRunner
from ap_agent.tools.mock_backends import MockBackends


def run(
    ctx: PhaseContext,
    state: RunState,
    emitter: EventEmitter,
    runner: ToolRunner,
    backends: MockBackends,
) -> None:
    """Count fraud indicators, then ask the model to read the evidence."""
    as_of = ctx.clock()
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
        vendor_summary=vendor_summary(state),
        order_summary=order_summary(state),
        history_summary=history_summary(state),
        nonce=nonce,
    )
    synthesis = ctx.call_model(state, emitter, prompt, EvidenceSynthesis)

    known_chunks = {chunk.chunk_id for chunk in state.all_chunks}
    state.add_facts(
        [
            SourcedFact(
                statement=fact.statement,
                value=fact.value,
                source="model synthesis of retrieved evidence",
                citations=resolve_citations(state, emitter, fact.citation_chunk_ids, known_chunks),
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
                citations=resolve_citations(
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
