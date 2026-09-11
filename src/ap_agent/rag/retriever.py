"""Retrieval with metadata-aware re-ranking.

This is where the corpus traps are handled, and it is worth being precise about why they are
handled here rather than anywhere else.

The corpus contains three documents designed to mislead:

``FIN-POL-003-OLD``  the authority matrix as it stood before 1 August 2026, with lower
                     limits. Lexically it is nearly identical to the current version: same
                     title, same vocabulary, same role names. A ranking function has no way
                     to prefer one over the other, because the thing that distinguishes them
                     is a date in the front matter.
``ADV-001``          a supplier notice instructing the reader to skip controls and pay. It
                     is topically relevant to an accounts-payable query, so it will rank.
``ADV-002``          a travel-expense extract full of plausible monetary limits that have
                     nothing to do with supplier invoices.

Three mechanisms address them, in order of strength:

1. **Type filtering.** A policy lookup asks for ``doc_types=("policy",)`` and cannot return
   anything else. This is what keeps ADV-002's meal allowances out of a tolerance question
   and ADV-001 out of the policy synthesis step. It is a filter, not a ranking preference,
   so no score can defeat it.
2. **Status demotion.** A superseded document's score is multiplied by a factor below one,
   configurable and defaulting to 0.3. Demotion rather than exclusion is deliberate: the
   document is legitimate history, an auditor may need it, and FIN-POL-003-OLD's own text
   asks a retrieval system to "rank current policy above this document and expose its
   superseded status".
3. **Labelling.** Every returned chunk carries its status, and every citation renders a
   non-current status visibly. The downstream prompt marks untrusted content as data.

Labelling is the weakest of the three and is never relied upon alone. Asking a model to
disregard a document it can read is a request, not a control. The filter is the control.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from ap_agent.domain.enums import DocumentStatus
from ap_agent.domain.errors import IndexNotBuilt
from ap_agent.domain.evidence import RetrievedChunk
from ap_agent.rag.dense import DenseEncoder, cosine_scores, fuse_by_rank
from ap_agent.rag.index import CorpusIndex
from ap_agent.rag.ingest import (
    DOC_TYPE_EXTERNAL_UNVERIFIED,
    DOC_TYPE_POLICY,
    DOC_TYPE_POLICY_SUPERSEDED,
    Chunk,
)

#: Policy lookups use this filter. Superseded and externally supplied documents are excluded
#: from policy synthesis by type, not by score.
POLICY_DOC_TYPES: Final[tuple[str, ...]] = (DOC_TYPE_POLICY,)

#: Evidence lookups deliberately include the untrusted class so that a supplier document
#: making a bank-change demand is found, labelled and routed to risk assessment. Not finding
#: it would be worse than finding it: an undetected injection attempt is an uncounted fraud
#: indicator.
EVIDENCE_DOC_TYPES: Final[tuple[str, ...]] = (
    DOC_TYPE_POLICY,
    DOC_TYPE_EXTERNAL_UNVERIFIED,
    DOC_TYPE_POLICY_SUPERSEDED,
)

#: Chunks scoring at or below this are dropped. BM25 assigns zero to a chunk sharing no
#: query term, and returning those pads the result with irrelevant citations that make a
#: recommendation look better sourced than it is.
MIN_RELEVANCE_SCORE: Final = 1e-9


@dataclass(frozen=True)
class RetrievalQuery:
    """A retrieval request. A value object so a run can log exactly what was asked."""

    text: str
    top_k: int = 6
    doc_types: tuple[str, ...] | None = None
    purpose: str = "evidence"


class Retriever:
    """Ranks corpus chunks for a query and returns them with citation metadata."""

    def __init__(
        self,
        index: CorpusIndex,
        *,
        superseded_score_factor: float = 0.3,
        default_top_k: int = 6,
        dense_encoder: DenseEncoder | None = None,
    ) -> None:
        if not 0.0 <= superseded_score_factor <= 1.0:
            raise ValueError("superseded_score_factor must be between 0 and 1")
        if dense_encoder is not None and not index.has_embeddings:
            # Refuse rather than degrade. A retriever asked for hybrid ranking that quietly
            # ranked lexically would reproduce the defect this parameter was added to fix: a
            # configured mode that changes nothing and reports success.
            raise IndexNotBuilt(
                "hybrid retrieval was requested but the index carries no embeddings; "
                "re-run `ap-agent ingest --force` with AP_RETRIEVAL_MODE=hybrid"
            )
        self._index = index
        self._superseded_factor = superseded_score_factor
        self._default_top_k = default_top_k
        self._dense = dense_encoder

    @property
    def index(self) -> CorpusIndex:
        return self._index

    @property
    def mode(self) -> str:
        """What this retriever actually does, not what was configured.

        Read by the manifest. The two can only agree if the dense side was really built, which
        is the point: the previous version reported the configured mode and had no dense side
        at all.
        """
        return "hybrid" if self._dense is not None else "bm25"

    def _base_scores(self, query: str) -> list[tuple[Chunk, float, float]]:
        """Per-chunk (chunk, lexical score, ranking score) before metadata adjustment.

        In lexical mode the two scores are the same number. In hybrid mode the ranking score
        is a reciprocal-rank fusion of the lexical and dense orderings, and chunks that
        neither retriever nominated are dropped rather than carried at a low score. The
        lexical score is still reported so a citation remains explainable in the terms the
        rest of the system uses.
        """
        lexical = self._index.score(query)
        if self._dense is None:
            return [(chunk, score, score) for chunk, score in lexical]

        query_vector = self._dense.encode([query], is_query=True)[0]
        dense_scores = cosine_scores(query_vector, self._index.embeddings)

        lexical_order = sorted(
            range(len(lexical)), key=lambda i: (-lexical[i][1], lexical[i][0].document_id)
        )
        dense_order = sorted(
            range(len(dense_scores)),
            key=lambda i: (-dense_scores[i], lexical[i][0].document_id),
        )
        fused = fuse_by_rank(lexical_order, dense_order)
        return [(lexical[i][0], lexical[i][1], score) for i, score in fused.items()]

    def _metadata_factor(self, chunk: Chunk) -> float:
        """Score multiplier derived from provenance rather than from text.

        Only the superseded class is demoted. Untrusted documents keep their lexical score
        because their retrieval is the point: they are evidence of what a supplier sent, and
        they are excluded from policy synthesis by the type filter instead.
        """
        if chunk.status is DocumentStatus.SUPERSEDED:
            return self._superseded_factor
        return 1.0

    def search(
        self,
        query: str,
        *,
        top_k: int | None = None,
        doc_types: Sequence[str] | None = None,
    ) -> list[RetrievedChunk]:
        """Return ranked chunks for a query.

        Ordering within equal scores is stabilised by document identifier and ordinal, so
        repeated runs over an unchanged corpus produce byte-identical citations. Without
        that, two runs of the same case could cite different sections and an auditor could
        not reproduce either.
        """
        limit = top_k if top_k is not None else self._default_top_k
        allowed = set(doc_types) if doc_types is not None else None

        scored: list[tuple[Chunk, float, float]] = []
        for chunk, lexical, ranking in self._base_scores(query):
            if allowed is not None and chunk.doc_type not in allowed:
                continue
            adjusted = ranking * self._metadata_factor(chunk)
            if adjusted <= MIN_RELEVANCE_SCORE:
                continue
            scored.append((chunk, lexical, adjusted))

        scored.sort(key=lambda item: (-item[2], item[0].document_id, item[0].ordinal))

        return [
            RetrievedChunk(
                chunk_id=chunk.chunk_id,
                document_id=chunk.document_id,
                title=chunk.title,
                version=chunk.version,
                status=chunk.status,
                classification=chunk.classification,
                effective_date=chunk.effective_date,
                section=chunk.section,
                text=chunk.text,
                doc_type=chunk.doc_type,
                lexical_score=round(lexical, 6),
                score=round(adjusted, 6),
                rank=position,
            )
            for position, (chunk, lexical, adjusted) in enumerate(scored[:limit], start=1)
        ]

    def search_policy(self, query: str, *, top_k: int | None = None) -> list[RetrievedChunk]:
        """Retrieve current policy only. Used wherever a rule needs a citation."""
        return self.search(query, top_k=top_k, doc_types=POLICY_DOC_TYPES)

    def search_evidence(self, query: str, *, top_k: int | None = None) -> list[RetrievedChunk]:
        """Retrieve across policy and supplier-supplied documents.

        Used to surface what a supplier has asserted, including adversarial content.
        """
        return self.search(query, top_k=top_k, doc_types=EVIDENCE_DOC_TYPES)
