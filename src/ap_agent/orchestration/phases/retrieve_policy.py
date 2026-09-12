"""Retrieve the governing policy sections the controls will cite.

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
    POLICY_QUERIES,
)
from ap_agent.rag.retriever import POLICY_DOC_TYPES
from ap_agent.tools.base import ToolRunner
from ap_agent.tools.contracts import (
    RETRIEVE_DOCUMENTS,
    RetrieveDocumentsInput,
    RetrieveDocumentsOutput,
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
    """Retrieve the current policy each control will cite."""
    handler = retrieve_finance_documents(ctx.retriever)
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
