"""Retrieval grounding tests.

Deterministic: no model, no network. They belong to the evaluation tier because they measure
quality against a golden set rather than asserting a single unit's behaviour, but they run in
continuous integration exactly like a unit test, which is the practical benefit of a lexical
retriever.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ap_agent.domain.enums import DocumentStatus
from ap_agent.domain.errors import IndexNotBuilt
from ap_agent.evaluation.retrieval import (
    CURRENT_AUTHORITY_ID,
    DISTRACTOR_DOCUMENT_ID,
    MIN_HIT_AT_3,
    MIN_MRR,
    SUPERSEDED_AUTHORITY_ID,
    evaluate_retrieval,
    load_golden_set,
)
from ap_agent.rag.index import CorpusIndex, build_index, tokenize
from ap_agent.rag.ingest import (
    DOC_TYPE_EXTERNAL_UNVERIFIED,
    DOC_TYPE_POLICY,
    DOC_TYPE_POLICY_SUPERSEDED,
    ingest_corpus,
)
from ap_agent.rag.retriever import EVIDENCE_DOC_TYPES, POLICY_DOC_TYPES, Retriever

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_DIR = REPO_ROOT / "finance_rag_corpus"
GOLDEN_PATH = Path(__file__).parent / "retrieval_golden.json"

ADVERSARIAL_DOCUMENT_ID = "ADV-001"


@pytest.fixture(scope="module")
def index() -> CorpusIndex:
    return CorpusIndex(ingest_corpus(CORPUS_DIR))


@pytest.fixture(scope="module")
def retriever(index: CorpusIndex) -> Retriever:
    return Retriever(index, superseded_score_factor=0.3, default_top_k=6)


# ---- ingestion ------------------------------------------------------------------------


class TestIngestion:
    def test_every_corpus_document_is_ingested(self) -> None:
        corpus = ingest_corpus(CORPUS_DIR)
        expected = len(list(CORPUS_DIR.glob("*.md")))
        assert corpus.document_count == expected
        assert len({chunk.document_id for chunk in corpus.chunks}) == expected

    def test_chunks_are_section_aligned_and_citable(self) -> None:
        corpus = ingest_corpus(CORPUS_DIR)
        for chunk in corpus.chunks:
            assert chunk.chunk_id
            assert chunk.document_id
            assert chunk.section.startswith("§")
            assert chunk.version
            assert chunk.text.strip()

    def test_section_labels_match_the_corpus_numbering(self) -> None:
        """A citation must be verifiable by opening the document and reading one section."""
        corpus = ingest_corpus(CORPUS_DIR)
        matching = [chunk for chunk in corpus.chunks if chunk.document_id == "FIN-POL-002"]
        sections = {chunk.section: chunk.heading for chunk in matching}
        assert sections["§2"] == "Tolerances"
        assert sections["§4"] == "Missing receipt"
        assert sections["§5"] == "Calculation rule"

    def test_document_types_are_derived_from_provenance_not_filename(self) -> None:
        corpus = ingest_corpus(CORPUS_DIR)
        by_id = {chunk.document_id: chunk for chunk in corpus.chunks}
        assert by_id["FIN-POL-002"].doc_type == DOC_TYPE_POLICY
        assert by_id[SUPERSEDED_AUTHORITY_ID].doc_type == DOC_TYPE_POLICY_SUPERSEDED
        assert by_id[ADVERSARIAL_DOCUMENT_ID].doc_type == DOC_TYPE_EXTERNAL_UNVERIFIED
        assert by_id[DISTRACTOR_DOCUMENT_ID].doc_type == DOC_TYPE_EXTERNAL_UNVERIFIED

    def test_the_adversarial_document_is_never_classified_as_policy(self) -> None:
        """ADV-001 asserts that a finance director approved it. Self-description is not
        provenance."""
        corpus = ingest_corpus(CORPUS_DIR)
        adversarial = [
            chunk for chunk in corpus.chunks if chunk.document_id == ADVERSARIAL_DOCUMENT_ID
        ]
        assert adversarial
        assert all(chunk.doc_type != DOC_TYPE_POLICY for chunk in adversarial)
        assert all(chunk.status is DocumentStatus.UNTRUSTED for chunk in adversarial)

    def test_superseded_status_and_date_are_preserved(self) -> None:
        corpus = ingest_corpus(CORPUS_DIR)
        stale = next(
            chunk for chunk in corpus.chunks if chunk.document_id == SUPERSEDED_AUTHORITY_ID
        )
        assert stale.status is DocumentStatus.SUPERSEDED
        assert stale.superseded_date is not None
        assert "superseded" in stale.citation_reference

    def test_corpus_hash_is_stable_across_runs(self) -> None:
        assert ingest_corpus(CORPUS_DIR).corpus_hash == ingest_corpus(CORPUS_DIR).corpus_hash

    def test_missing_corpus_directory_fails_clearly(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            ingest_corpus(tmp_path / "nope")

    def test_empty_corpus_directory_fails_clearly(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="no Markdown documents"):
            ingest_corpus(tmp_path)


# ---- tokenization ---------------------------------------------------------------------


class TestTokenizer:
    def test_identifiers_keep_their_whole_form(self) -> None:
        tokens = tokenize("FIN-POL-003")
        assert "fin-pol-003" in tokens
        assert "fin" in tokens

    def test_ordinary_compounds_are_split_without_the_whole_form(self) -> None:
        """Emitting both inflates term frequency for common hyphenated phrases."""
        assert tokenize("purchase-order") == ["purchase", "order"]

    def test_plural_forms_collapse_to_the_singular(self) -> None:
        assert tokenize("invoices") == tokenize("invoice")
        assert tokenize("tolerances") == tokenize("tolerance")

    def test_negation_is_preserved(self) -> None:
        """ "must not" differs from "must"; the tokenizer does not discard the qualifier."""
        assert "not" in tokenize("the processor must not approve")

    def test_query_terms_are_deduplicated(self, index: CorpusIndex) -> None:
        """BM25 scores over the query term set; a repeated token must not count twice."""
        terms = index.query_terms("purchase order and purchase requisition")
        assert terms.count("purchase") == 1


# ---- index ----------------------------------------------------------------------------


class TestIndex:
    def test_index_round_trips_through_disk(self, tmp_path: Path) -> None:
        built, rebuilt = build_index(CORPUS_DIR, tmp_path / "index")
        assert rebuilt
        loaded = CorpusIndex.load(tmp_path / "index")
        assert loaded.corpus_hash == built.corpus_hash
        assert len(loaded) == len(built)

    def test_rebuild_is_skipped_when_the_corpus_is_unchanged(self, tmp_path: Path) -> None:
        """Ingestion is safe to run on every start."""
        build_index(CORPUS_DIR, tmp_path / "index")
        _, rebuilt = build_index(CORPUS_DIR, tmp_path / "index")
        assert not rebuilt

    def test_force_rebuilds_even_when_unchanged(self, tmp_path: Path) -> None:
        build_index(CORPUS_DIR, tmp_path / "index")
        _, rebuilt = build_index(CORPUS_DIR, tmp_path / "index", force=True)
        assert rebuilt

    def test_loading_an_absent_index_is_actionable(self, tmp_path: Path) -> None:
        with pytest.raises(IndexNotBuilt, match="ingest"):
            CorpusIndex.load(tmp_path / "missing")

    def test_index_is_stored_as_json_not_a_pickle(self, tmp_path: Path) -> None:
        """Loading a pickle at startup would deserialise executable content from disk."""
        build_index(CORPUS_DIR, tmp_path / "index")
        written = list((tmp_path / "index").iterdir())
        assert written
        assert all(path.suffix == ".json" for path in written)


# ---- trap handling --------------------------------------------------------------------


class TestDistractorIsFilteredByType:
    """Mechanism 1: a filter, which no score can defeat."""

    def test_the_distractor_never_appears_in_a_policy_search(self, retriever: Retriever) -> None:
        for query in [
            "meal allowance limit",
            "daily limit for dinner",
            "AUD 75 limit",
            "what monetary limits apply",
            "travel",
        ]:
            results = retriever.search_policy(query, top_k=10)
            assert all(chunk.document_id != DISTRACTOR_DOCUMENT_ID for chunk in results), query

    def test_the_distractor_is_reachable_on_its_own_topic_when_unfiltered(
        self, retriever: Retriever
    ) -> None:
        """The filter is scoped, not a blanket ban: the document still exists in the index."""
        results = retriever.search("breakfast lunch dinner reimbursable alcohol", top_k=10)
        assert any(chunk.document_id == DISTRACTOR_DOCUMENT_ID for chunk in results)

    def test_policy_and_evidence_filters_differ_as_designed(self) -> None:
        assert DISTRACTOR_DOCUMENT_ID not in POLICY_DOC_TYPES
        assert DOC_TYPE_EXTERNAL_UNVERIFIED in EVIDENCE_DOC_TYPES
        assert DOC_TYPE_EXTERNAL_UNVERIFIED not in POLICY_DOC_TYPES


class TestSupersededPolicyIsDemoted:
    """Mechanism 2: demotion by metadata, because no ranking function knows about dates."""

    def test_current_authority_outranks_the_superseded_matrix(self, retriever: Retriever) -> None:
        for query in [
            "delegated financial authority matrix limits",
            "maximum approval for a cost centre manager",
            "CFO approval limit",
            "department director limit",
        ]:
            results = retriever.search(query, top_k=10)
            current = next((c.rank for c in results if c.document_id == CURRENT_AUTHORITY_ID), None)
            stale = next(
                (c.rank for c in results if c.document_id == SUPERSEDED_AUTHORITY_ID), None
            )
            assert current is not None, query
            if stale is not None:
                assert current < stale, f"{query}: stale at {stale}, current at {current}"

    def test_superseded_policy_is_excluded_from_policy_searches_entirely(
        self, retriever: Retriever
    ) -> None:
        results = retriever.search_policy("delegated authority limits", top_k=10)
        assert all(chunk.document_id != SUPERSEDED_AUTHORITY_ID for chunk in results)

    def test_demotion_reduces_the_score_but_preserves_the_lexical_score(
        self, retriever: Retriever
    ) -> None:
        """Both scores are returned so a test can prove demotion happened."""
        results = retriever.search("delegated financial authority matrix", top_k=20)
        stale = next((c for c in results if c.document_id == SUPERSEDED_AUTHORITY_ID), None)
        assert stale is not None
        assert stale.score < stale.lexical_score
        assert stale.score == pytest.approx(stale.lexical_score * 0.3, rel=1e-3)

    def test_a_factor_of_one_disables_demotion(self, index: CorpusIndex) -> None:
        undemoted = Retriever(index, superseded_score_factor=1.0)
        results = undemoted.search("delegated financial authority matrix", top_k=20)
        stale = next(c for c in results if c.document_id == SUPERSEDED_AUTHORITY_ID)
        assert stale.score == pytest.approx(stale.lexical_score, rel=1e-6)

    def test_invalid_demotion_factor_is_rejected(self, index: CorpusIndex) -> None:
        with pytest.raises(ValueError, match="between 0 and 1"):
            Retriever(index, superseded_score_factor=1.5)


class TestAdversarialDocumentIsFoundAndLabelled:
    """Mechanism 3: labelling. The adversarial document must be retrievable as evidence."""

    def test_the_adversarial_document_is_excluded_from_policy_searches(
        self, retriever: Retriever
    ) -> None:
        for query in [
            "bank account has changed pay immediately",
            "urgent payment instructions",
            "supplier bank change",
        ]:
            results = retriever.search_policy(query, top_k=10)
            assert all(chunk.document_id != ADVERSARIAL_DOCUMENT_ID for chunk in results), query

    def test_the_adversarial_document_is_found_by_an_evidence_search(
        self, retriever: Retriever
    ) -> None:
        """Not finding it would be worse: an undetected injection is an uncounted indicator."""
        results = retriever.search_evidence("urgent bank account change new account", top_k=10)
        assert any(chunk.document_id == ADVERSARIAL_DOCUMENT_ID for chunk in results)

    def test_every_retrieved_untrusted_chunk_is_labelled_untrusted(
        self, retriever: Retriever
    ) -> None:
        results = retriever.search_evidence("urgent bank account change", top_k=10)
        adversarial = [c for c in results if c.document_id == ADVERSARIAL_DOCUMENT_ID]
        assert adversarial
        for chunk in adversarial:
            assert chunk.status is DocumentStatus.UNTRUSTED
            assert chunk.doc_type == DOC_TYPE_EXTERNAL_UNVERIFIED
            assert "untrusted" in chunk.to_citation().reference

    def test_retrieved_chunks_are_always_data_never_policy(self, retriever: Retriever) -> None:
        """Even a current internal policy chunk is untrusted evidence until trusted code
        decides what to do with it."""
        from ap_agent.domain.enums import TrustLevel

        results = retriever.search_policy("three-way matching tolerance", top_k=5)
        assert results
        assert all(chunk.trust is TrustLevel.UNTRUSTED_EVIDENCE for chunk in results)


# ---- ranking behaviour ----------------------------------------------------------------


class TestRankingBehaviour:
    def test_results_are_ranked_from_one_with_descending_scores(self, retriever: Retriever) -> None:
        results = retriever.search_policy("tolerance", top_k=5)
        assert [chunk.rank for chunk in results] == list(range(1, len(results) + 1))
        scores = [chunk.score for chunk in results]
        assert scores == sorted(scores, reverse=True)

    def test_zero_relevance_chunks_are_not_returned(self, retriever: Retriever) -> None:
        """Padding results makes a recommendation look better sourced than it is."""
        results = retriever.search_policy("zzzz nonexistent terminology qqq", top_k=10)
        assert results == []

    def test_ranking_is_deterministic_across_repeated_searches(self, retriever: Retriever) -> None:
        """An auditor must be able to reproduce a run's citations exactly."""
        first = retriever.search_policy("vendor bank account change", top_k=6)
        second = retriever.search_policy("vendor bank account change", top_k=6)
        assert [c.chunk_id for c in first] == [c.chunk_id for c in second]
        assert [c.score for c in first] == [c.score for c in second]

    def test_top_k_is_respected(self, retriever: Retriever) -> None:
        assert len(retriever.search_policy("invoice", top_k=3)) <= 3

    def test_an_empty_query_returns_nothing(self, retriever: Retriever) -> None:
        assert retriever.search_policy("", top_k=5) == []


# ---- golden set -----------------------------------------------------------------------


class TestGoldenSet:
    def test_golden_set_gates_are_met(self, retriever: Retriever) -> None:
        report = evaluate_retrieval(retriever, load_golden_set(GOLDEN_PATH), top_k=6)
        assert report.hit_at_3 >= MIN_HIT_AT_3, report.summary_line()
        assert report.mean_reciprocal_rank >= MIN_MRR, report.summary_line()
        assert report.safety_checks_passed, report.summary_line()
        assert report.gates_passed, report.summary_line()

    def test_every_golden_query_retrieves_its_target_within_three(
        self, retriever: Retriever
    ) -> None:
        report = evaluate_retrieval(retriever, load_golden_set(GOLDEN_PATH), top_k=6)
        misses = [outcome.query_id for outcome in report.outcomes if not outcome.hit_at_3]
        assert misses == [], f"queries missing their target in the top 3: {misses}"

    def test_every_returned_chunk_carries_full_citation_metadata(
        self, retriever: Retriever
    ) -> None:
        report = evaluate_retrieval(retriever, load_golden_set(GOLDEN_PATH), top_k=6)
        assert report.missing_citation_metadata == []

    def test_golden_set_covers_the_policies_the_agent_relies_on(self) -> None:
        """A golden set that omits a policy the engine cites is not measuring the system."""
        queries = load_golden_set(GOLDEN_PATH)
        covered = {reference.split()[0] for query in queries for reference in query.accept}
        for required in [
            "FIN-POL-001",
            "FIN-POL-002",
            "FIN-POL-003",
            "FIN-POL-004",
            "FIN-POL-005",
            "FIN-POL-007",
            "FIN-POL-009",
        ]:
            assert required in covered, f"{required} is not covered by the golden set"
