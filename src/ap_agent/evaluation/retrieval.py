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

## Three kinds of query, measured separately

An earlier version of this harness had one kind: a question phrased by the author, with the
answering section named. Sixteen of those measured what the retriever is good at and could not
fail for the reason lexical retrieval actually fails.

- ``direct`` queries use the corpus's own vocabulary. These carry the gate, because a
  regression here is a defect.
- ``paraphrase`` queries ask the same things in a finance analyst's words, deliberately
  avoiding the section's terms. These are the declared weakness of a lexical index, so they are
  measured and reported with their own, much lower, gate. Averaging them into the headline
  would hide both numbers: it would flatter the paraphrase result and understate the direct one.
- ``negative`` queries are questions the corpus cannot answer. They have no accepted section,
  and what is checked is that the distractor never surfaces.

**On score floors.** The obvious use for negative queries is to calibrate a minimum score below
which the retriever returns nothing. The measurement says that does not work here: the highest
scoring unanswerable query outscores the lowest scoring answerable one, so any floor that
silenced the first would silence the second. The report states both figures and
``score_floor_separable``, so the idea is refuted by data in the output rather than re-proposed
every time someone reads the negative results.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

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

#: Gate for paraphrase recall, which is a different question from the one above. Measured at
#: 0.38 on the eight-query paraphrase set: four of the eight miss the answering section
#: entirely. The gate is set below that to catch a *regression* in paraphrase handling, not to
#: pretend the current figure is good. It is the number that would justify turning on hybrid
#: retrieval, and the number that would have to move before anyone claims the lexical index is
#: sufficient on its own.
MIN_PARAPHRASE_HIT_AT_3: Final = 0.30

#: Query kinds. See the module docstring for why they are measured apart.
QueryKind = Literal["direct", "paraphrase", "negative"]


class QueryOutcome(BaseModel):
    """Per-query retrieval result."""

    model_config = ConfigDict(extra="forbid")

    query_id: str
    kind: QueryKind = "direct"
    text: str
    accept: list[str]
    retrieved: list[str]
    hit_rank: int | None
    hit_at_1: bool
    hit_at_3: bool
    reciprocal_rank: float
    #: Score of the top result, or zero when nothing was returned. Recorded for every query
    #: because the answerable and unanswerable distributions are what refute a score floor.
    top_score: float = 0.0

    @property
    def answerable(self) -> bool:
        return self.kind != "negative"


class RetrievalReport(BaseModel):
    """Aggregate retrieval quality plus the absolute safety checks.

    ``hit_at_1``, ``hit_at_3`` and ``mean_reciprocal_rank`` cover the ``direct`` queries only.
    That keeps the gated numbers comparable across changes and keeps their meaning: a fall
    there is a regression in the retriever, whereas a fall in paraphrase recall may only mean
    the paraphrase set got harder. Both are reported.
    """

    model_config = ConfigDict(extra="forbid")

    query_count: int
    hit_at_1: float
    hit_at_3: float
    mean_reciprocal_rank: float
    #: Paraphrase recall, reported separately. See the module docstring.
    paraphrase_count: int = 0
    paraphrase_hit_at_3: float = 0.0
    #: Unanswerable queries, and whether the distractor stayed out of them.
    negative_count: int = 0
    #: Lowest top-score across answerable queries, and highest across unanswerable ones. When
    #: the second exceeds the first, no score floor can separate them.
    answerable_min_top_score: float = 0.0
    negative_max_top_score: float = 0.0
    distractor_leaks: list[str] = Field(default_factory=list)
    stale_policy_leaks: list[str] = Field(default_factory=list)
    missing_citation_metadata: list[str] = Field(default_factory=list)
    outcomes: list[QueryOutcome] = Field(default_factory=list)

    @property
    def score_floor_separable(self) -> bool:
        """Whether a minimum-score cutoff could tell answerable from unanswerable queries."""
        if not self.negative_count:
            return False
        return self.negative_max_top_score < self.answerable_min_top_score

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
            and (self.paraphrase_count == 0 or self.paraphrase_hit_at_3 >= MIN_PARAPHRASE_HIT_AT_3)
        )

    def summary_line(self) -> str:
        verdict = "PASS" if self.gates_passed else "FAIL"
        parts = [
            f"retrieval {verdict}: direct Hit@1={self.hit_at_1:.2f} "
            f"Hit@3={self.hit_at_3:.2f} MRR={self.mean_reciprocal_rank:.3f} "
            f"over {self.query_count} queries"
        ]
        if self.paraphrase_count:
            parts.append(
                f"paraphrase Hit@3={self.paraphrase_hit_at_3:.2f} over {self.paraphrase_count}"
            )
        if self.negative_count:
            parts.append(
                f"{self.negative_count} unanswerable, score floor "
                f"{'separable' if self.score_floor_separable else 'not separable'} "
                f"(answerable min {self.answerable_min_top_score:.2f} vs unanswerable max "
                f"{self.negative_max_top_score:.2f})"
            )
        parts.append(
            f"{len(self.distractor_leaks)} distractor leak(s), "
            f"{len(self.stale_policy_leaks)} stale-policy leak(s)"
        )
        return ", ".join(parts)


class GoldenQuery(BaseModel):
    """One golden-set entry.

    Validated on load rather than read as a raw mapping, so a malformed golden set fails
    with a field-level message instead of an attribute error partway through a measurement
    run.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    text: str
    kind: QueryKind = "direct"
    #: Every section that answers the query. A hit is any of them, because several corpus
    #: sections legitimately answer some questions. Empty for an unanswerable query, and
    #: required for every other kind.
    accept: list[str] = Field(default_factory=list)
    purpose: str = ""

    @model_validator(mode="after")
    def _accept_matches_kind(self) -> Self:
        """An answerable query needs an answer; an unanswerable one must not name one.

        Checked here because the alternative is a silent measurement error: a negative query
        that carried an ``accept`` list would be scored as a miss on every run and quietly drag
        the headline down, and an answerable query with no ``accept`` could never be a hit.
        """
        if self.kind == "negative" and self.accept:
            raise ValueError(f"{self.id}: a negative query must not name an accepted section")
        if self.kind != "negative" and not self.accept:
            raise ValueError(f"{self.id}: a {self.kind} query must name at least one section")
        return self


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
                kind=entry.kind,
                text=text,
                accept=accept,
                retrieved=references,
                hit_rank=hit_rank,
                hit_at_1=hit_rank == 1,
                hit_at_3=hit_rank is not None and hit_rank <= 3,
                reciprocal_rank=(1.0 / hit_rank) if hit_rank else 0.0,
                top_score=results[0].score if results else 0.0,
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

    direct = [outcome for outcome in outcomes if outcome.kind == "direct"]
    paraphrase = [outcome for outcome in outcomes if outcome.kind == "paraphrase"]
    negative = [outcome for outcome in outcomes if outcome.kind == "negative"]
    if not direct:
        raise ValueError("the golden set must contain at least one direct query to gate on")

    answerable_scores = [outcome.top_score for outcome in outcomes if outcome.answerable]
    negative_scores = [outcome.top_score for outcome in negative]

    return RetrievalReport(
        query_count=len(direct),
        hit_at_1=sum(outcome.hit_at_1 for outcome in direct) / len(direct),
        hit_at_3=sum(outcome.hit_at_3 for outcome in direct) / len(direct),
        mean_reciprocal_rank=sum(outcome.reciprocal_rank for outcome in direct) / len(direct),
        paraphrase_count=len(paraphrase),
        paraphrase_hit_at_3=(
            sum(outcome.hit_at_3 for outcome in paraphrase) / len(paraphrase) if paraphrase else 0.0
        ),
        negative_count=len(negative),
        answerable_min_top_score=min(answerable_scores) if answerable_scores else 0.0,
        negative_max_top_score=max(negative_scores) if negative_scores else 0.0,
        distractor_leaks=distractor_leaks,
        stale_policy_leaks=stale_leaks,
        missing_citation_metadata=missing_metadata,
        outcomes=outcomes,
    )
