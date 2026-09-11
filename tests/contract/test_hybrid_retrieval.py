"""Hybrid retrieval wiring, tested without a model download.

The defect these cover was not a bug in dense retrieval. It was that `AP_RETRIEVAL_MODE=hybrid`
validated, appeared in the manifest, was documented in six places including a manifest row
naming its implementation file, and reached no code at all. Turning the flag on changed
nothing and reported success.

So the contract asserted here is narrow and specific: a retriever asked for hybrid ranking
either ranks with a dense side or refuses. It never quietly ranks lexically, because a silent
fallback is exactly how the original defect stayed invisible.

The encoder is a stub. A real one costs a 100 MB download, which would make these tests the
kind that get skipped, and none of the behaviour here depends on the embeddings being good.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ap_agent.domain.errors import IndexNotBuilt
from ap_agent.rag.dense import DenseEncoder
from ap_agent.rag.index import CorpusIndex
from ap_agent.rag.ingest import ingest_corpus
from ap_agent.rag.retriever import Retriever

CORPUS_DIR = Path(__file__).resolve().parents[2] / "finance_rag_corpus"


class StubEncoder(DenseEncoder):
    """An encoder with a fixed, deterministic embedding rule and no model.

    Each text is embedded as two dimensions: whether it mentions "receipt", and whether it
    mentions "approval". Crude on purpose. It gives the dense side a defensible opinion that
    differs from BM25's, which is all these tests need to prove fusion is wired.
    """

    def __init__(self) -> None:
        super().__init__(model_name="stub-encoder")

    def encode(self, texts, *, is_query: bool = False):  # type: ignore[no-untyped-def]
        vectors = []
        for text in texts:
            lowered = text.lower()
            first = 1.0 if "receipt" in lowered else 0.0
            second = 1.0 if "approval" in lowered else 0.0
            if first == 0.0 and second == 0.0:
                first = 0.001
            norm = (first**2 + second**2) ** 0.5
            vectors.append([first / norm, second / norm])
        return vectors


@pytest.fixture(scope="module")
def corpus():
    return ingest_corpus(CORPUS_DIR)


class TestTheFlagCannotBeSilentlyIgnored:
    def test_hybrid_against_an_index_without_embeddings_refuses(self, corpus) -> None:
        """The guard that makes the original defect impossible to reintroduce."""
        index = CorpusIndex(corpus)
        with pytest.raises(IndexNotBuilt, match="no embeddings"):
            Retriever(index, dense_encoder=StubEncoder())

    def test_reading_the_matrix_from_an_index_without_one_refuses(self, corpus) -> None:
        """An empty matrix would make every similarity zero and rank lexically by accident."""
        index = CorpusIndex(corpus)
        assert not index.has_embeddings
        with pytest.raises(IndexNotBuilt):
            _ = index.embeddings

    def test_an_embedding_count_mismatch_refuses_at_construction(self, corpus) -> None:
        with pytest.raises(ValueError, match="embeddings"):
            CorpusIndex(corpus, embeddings=[[1.0, 0.0]], embedding_model="stub-encoder")


class TestModeIsReportedFromBehaviourNotConfiguration:
    def test_a_lexical_retriever_reports_bm25(self, corpus) -> None:
        assert Retriever(CorpusIndex(corpus)).mode == "bm25"

    def test_a_hybrid_retriever_reports_hybrid(self, corpus) -> None:
        encoder = StubEncoder()
        index = CorpusIndex(
            corpus,
            embeddings=encoder.encode([chunk.text for chunk in corpus.chunks]),
            embedding_model=encoder.model_name,
        )
        assert Retriever(index, dense_encoder=encoder).mode == "hybrid"


class TestFusionChangesTheRanking:
    @staticmethod
    def _both(corpus) -> tuple[Retriever, Retriever]:
        encoder = StubEncoder()
        lexical_index = CorpusIndex(corpus)
        hybrid_index = CorpusIndex(
            corpus,
            embeddings=encoder.encode([chunk.text for chunk in corpus.chunks]),
            embedding_model=encoder.model_name,
        )
        return (
            Retriever(lexical_index, default_top_k=6),
            Retriever(hybrid_index, default_top_k=6, dense_encoder=encoder),
        )

    def test_the_two_modes_do_not_return_identical_rankings(self, corpus) -> None:
        """The test that would have caught the original defect immediately."""
        lexical, hybrid = self._both(corpus)
        query = "goods receipt recorded before approval"
        lexical_ids = [chunk.chunk_id for chunk in lexical.search_policy(query)]
        hybrid_ids = [chunk.chunk_id for chunk in hybrid.search_policy(query)]
        assert lexical_ids != hybrid_ids

    def test_hybrid_still_reports_the_lexical_score_for_each_citation(self, corpus) -> None:
        """A citation stays explainable in the terms the rest of the system uses."""
        _, hybrid = self._both(corpus)
        results = hybrid.search_policy("goods receipt recorded before approval")
        assert results
        assert all(chunk.lexical_score >= 0.0 for chunk in results)

    def test_hybrid_still_excludes_the_distractor_from_policy_results(self, corpus) -> None:
        """Type filtering is a filter, so no score from either retriever can defeat it."""
        _, hybrid = self._both(corpus)
        results = hybrid.search_policy("mileage rate for a personal car")
        assert all(chunk.document_id != "ADV-002" for chunk in results)

    def test_hybrid_still_demotes_the_superseded_authority_matrix(self, corpus) -> None:
        """Metadata demotion multiplies the fused score, so it survives the change of base."""
        _, hybrid = self._both(corpus)
        results = hybrid.search_policy("department director approval limit")
        ranks = {chunk.document_id: chunk.rank for chunk in results}
        if "FIN-POL-003-OLD" in ranks and "FIN-POL-003" in ranks:
            assert ranks["FIN-POL-003"] < ranks["FIN-POL-003-OLD"]

    def test_hybrid_returns_no_more_than_the_requested_window(self, corpus) -> None:
        """Fusion nominates twenty candidates; the caller asked for six."""
        _, hybrid = self._both(corpus)
        assert len(hybrid.search_policy("approval", top_k=6)) <= 6
