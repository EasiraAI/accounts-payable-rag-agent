"""Retrieval quality measurement.

Lives in the package rather than only in the test suite so that the same numbers are
available from the command line and the HTTP evaluation endpoint. A quality measure that
only exists inside a test run is one nobody looks at after it first passes.

## Which metrics, and why

**Hit@3 and MRR are the operative measures.** The agent consumes a top-k window of chunks,
not a single best result, so a target section at rank 2 alongside the rest of the relevant
policy is a successful retrieval for this architecture. Gating on Hit@1 would optimise for a
consumption pattern the system does not have, and would push towards over-fitting the
ranking to the golden set.

**Hit@1 is reported but not gated.** It is useful as a trend: a fall in Hit@1 with Hit@3
unchanged means the ordering inside the window drifted, which is worth knowing before it
becomes a recall problem.

**Two safety checks are absolute, not statistical.** The distractor must never appear in a
policy result, and the current authority matrix must always outrank the superseded one.
These are pass or fail, because an average that tolerates one leak is not a control.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from ap_agent.rag.retriever import Retriever

#: The distractor: an internally plausible travel-expense policy with monetary limits that
#: have nothing to do with supplier invoices.
DISTRACTOR_DOCUMENT_ID: Final = "ADV-002"

#: The superseded authority matrix and the current one that must outrank it.
SUPERSEDED_AUTHORITY_ID: Final = "FIN-POL-003-OLD"
CURRENT_AUTHORITY_ID: Final = "FIN-POL-003"

#: Gates. Set from measured behaviour rather than aspiration, with headroom so that a small
#: corpus change does not fail the build, and tight enough that a real regression does.
#: Measured on the 16-query golden set at the time of writing: Hit@1 0.94, Hit@3 1.00,
#: MRR 0.969. The gates sit below those figures by roughly one query's worth of margin.
MIN_HIT_AT_3: Final = 0.90
MIN_MRR: Final = 0.88


class QueryOutcome(BaseModel):
    """Per-query retrieval result."""

    model_config = ConfigDict(extra="forbid")

    query_id: str
    text: str
    accept: list[str]
    retrieved: list[str]
    hit_rank: int | None
    hit_at_1: bool
    hit_at_3: bool
    reciprocal_rank: float


class RetrievalReport(BaseModel):
    """Aggregate retrieval quality plus the absolute safety checks."""

    model_config = ConfigDict(extra="forbid")

    query_count: int
    hit_at_1: float
    hit_at_3: float
    mean_reciprocal_rank: float
    distractor_leaks: list[str] = Field(default_factory=list)
    stale_policy_leaks: list[str] = Field(default_factory=list)
    missing_citation_metadata: list[str] = Field(default_factory=list)
    outcomes: list[QueryOutcome] = Field(default_factory=list)

    @property
    def safety_checks_passed(self) -> bool:
        return not (
            self.distractor_leaks or self.stale_policy_leaks or self.missing_citation_metadata
        )

    @property
    def gates_passed(self) -> bool:
        return (
            self.safety_checks_passed
            and self.hit_at_3 >= MIN_HIT_AT_3
            and self.mean_reciprocal_rank >= MIN_MRR
        )

    def summary_line(self) -> str:
        verdict = "PASS" if self.gates_passed else "FAIL"
        return (
            f"retrieval {verdict}: Hit@1={self.hit_at_1:.2f} Hit@3={self.hit_at_3:.2f} "
            f"MRR={self.mean_reciprocal_rank:.3f} over {self.query_count} queries, "
            f"{len(self.distractor_leaks)} distractor leak(s), "
            f"{len(self.stale_policy_leaks)} stale-policy leak(s)"
        )


class GoldenQuery(BaseModel):
    """One golden-set entry.

    Validated on load rather than read as a raw mapping, so a malformed golden set fails
    with a field-level message instead of an attribute error partway through a measurement
    run.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    text: str
    #: Every section that answers the query. A hit is any of them, because several corpus
    #: sections legitimately answer some questions.
    accept: list[str] = Field(min_length=1)
    purpose: str = ""


def load_golden_set(path: Path) -> list[GoldenQuery]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    queries = payload.get("queries")
    if not isinstance(queries, list) or not queries:
        raise ValueError(f"no queries in golden set {path}")
    return [GoldenQuery.model_validate(entry) for entry in queries]


def evaluate_retrieval(
    retriever: Retriever,
    golden_queries: list[GoldenQuery],
    *,
    top_k: int = 6,
) -> RetrievalReport:
    """Measure retrieval against the golden set and run the safety checks."""
    outcomes: list[QueryOutcome] = []
    distractor_leaks: list[str] = []
    stale_leaks: list[str] = []
    missing_metadata: list[str] = []

    for entry in golden_queries:
        query_id = entry.id
        text = entry.text
        accept = entry.accept

        results = retriever.search_policy(text, top_k=top_k)
        references = [f"{chunk.document_id} {chunk.section}" for chunk in results]

        hit_rank: int | None = None
        for position, reference in enumerate(references, start=1):
            if reference in accept:
                hit_rank = position
                break

        outcomes.append(
            QueryOutcome(
                query_id=query_id,
                text=text,
                accept=accept,
                retrieved=references,
                hit_rank=hit_rank,
                hit_at_1=hit_rank == 1,
                hit_at_3=hit_rank is not None and hit_rank <= 3,
                reciprocal_rank=(1.0 / hit_rank) if hit_rank else 0.0,
            )
        )

        # Safety check 1: the distractor must never reach a policy result.
        if any(chunk.document_id == DISTRACTOR_DOCUMENT_ID for chunk in results):
            distractor_leaks.append(f"{query_id}: {text}")

        # Safety check 2: when both authority matrices are retrieved, the current one wins.
        current_rank = next(
            (chunk.rank for chunk in results if chunk.document_id == CURRENT_AUTHORITY_ID), None
        )
        stale_rank = next(
            (chunk.rank for chunk in results if chunk.document_id == SUPERSEDED_AUTHORITY_ID),
            None,
        )
        if stale_rank is not None and (current_rank is None or stale_rank < current_rank):
            stale_leaks.append(
                f"{query_id}: superseded at rank {stale_rank}, current at {current_rank}"
            )

        # Safety check 3: every chunk must be citable. A result without provenance cannot be
        # used as a sourced fact, so an incomplete chunk is a pipeline defect, not a warning.
        for chunk in results:
            if not (chunk.document_id and chunk.version and chunk.section and chunk.chunk_id):
                missing_metadata.append(f"{query_id}: {chunk.chunk_id or '<no id>'}")

    count = len(outcomes)
    return RetrievalReport(
        query_count=count,
        hit_at_1=sum(outcome.hit_at_1 for outcome in outcomes) / count,
        hit_at_3=sum(outcome.hit_at_3 for outcome in outcomes) / count,
        mean_reciprocal_rank=sum(outcome.reciprocal_rank for outcome in outcomes) / count,
        distractor_leaks=distractor_leaks,
        stale_policy_leaks=stale_leaks,
        missing_citation_metadata=missing_metadata,
        outcomes=outcomes,
    )
