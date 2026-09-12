"""Turning the identifiers a model returns into citations, and the run's own citation list.

Separated from the orchestrator because this is the join between what was retrieved and what a
recommendation claims, and it is the mechanism behind one of the system's safety properties: a
model cannot present a citation to a document it was not shown. It returns chunk identifiers,
and they are resolved here against the chunks this run actually retrieved. An identifier that
resolves to nothing is dropped and recorded, never rendered.
"""

from __future__ import annotations

from collections.abc import Sequence

from ap_agent.domain.enums import (
    EventType,
)
from ap_agent.domain.evidence import Citation
from ap_agent.domain.run_state import RunState
from ap_agent.observability.events import EventEmitter


def resolve_citations(
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


def recommendation_citations(state: RunState) -> list[Citation]:
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
