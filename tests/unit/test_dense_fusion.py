"""Rank fusion and similarity, tested without loading a model.

The fusion is where a ranking bug would hide, and a test that needs a 100 MB download is a
test that gets skipped. Everything here operates on integer orderings and plain lists, so the
whole file runs in the unit tier with no optional dependency present.

Context for why this module exists at all: `AP_RETRIEVAL_MODE=hybrid` was a documented,
validated setting that reached no code. An audit called the mode unmeasured; it was absent.
These tests cover the behaviour that now stands behind the flag.
"""

from __future__ import annotations

import math

from ap_agent.rag.dense import (
    CANDIDATE_DEPTH,
    RRF_K,
    cosine_scores,
    fuse_by_rank,
)


class TestFuseByRank:
    def test_a_chunk_ranked_first_by_both_retrievers_scores_highest(self) -> None:
        fused = fuse_by_rank([0, 1, 2], [0, 2, 1])
        assert max(fused, key=lambda index: fused[index]) == 0

    def test_the_score_is_the_sum_of_reciprocal_ranks(self) -> None:
        """Stated explicitly so the constant cannot drift without a test noticing."""
        fused = fuse_by_rank([5], [5])
        assert math.isclose(fused[5], 2.0 / (RRF_K + 1))

    def test_a_chunk_nominated_by_one_retriever_still_scores(self) -> None:
        """The point of fusing: dense can rescue what lexical missed, and the reverse."""
        fused = fuse_by_rank([0], [1])
        assert set(fused) == {0, 1}

    def test_a_chunk_nominated_by_neither_is_absent_rather_than_zero(self) -> None:
        """Absent, not zero-scored.

        A cosine similarity is never exactly zero, so without the candidate cutoff every
        chunk in the corpus would earn some contribution and the result would be padded with
        sections that answer nothing. Six citations look better sourced than two whether or
        not the extra four are relevant, which makes padding worse than a short answer.
        """
        fused = fuse_by_rank([0, 1], [0, 1])
        assert 2 not in fused

    def test_only_the_head_of_each_list_contributes(self) -> None:
        deep = list(range(CANDIDATE_DEPTH + 5))
        fused = fuse_by_rank(deep, deep)
        assert len(fused) == CANDIDATE_DEPTH
        assert CANDIDATE_DEPTH not in fused

    def test_two_agreeing_retrievers_preserve_their_shared_order(self) -> None:
        fused = fuse_by_rank([3, 1, 2], [3, 1, 2])
        order = sorted(fused, key=lambda index: -fused[index])
        assert order == [3, 1, 2]

    def test_consensus_does_not_beat_a_polarised_pair_at_this_k(self) -> None:
        """Not the behaviour I first assumed, and the arithmetic is worth pinning down.

        Lexical loves 0 and hates 9; dense loves 9 and hates 0; both put 5 in the middle. The
        intuition is that the consensus choice should win. It does not: 1/61 + 1/63 is
        0.032266 and 2/62 is 0.032258, so a first-and-third placing edges out second-and-second
        by eight parts in a million.

        That is a property of the published k=60, which was calibrated for result lists a
        thousand deep. Over a twenty-candidate head it compresses every fused score into the
        third decimal place, so the ordering is decided by margins far smaller than the
        retrievers' own confidence. It is also the best available explanation for the measured
        cost of hybrid mode on this corpus: fusion throws away BM25's score *margin*, and on
        terminology-dense policy text that margin carries real signal, which is why direct
        Hit@3 falls from 1.00 to 0.94 when the dense side is switched on.

        k is left at the published value rather than fitted to sixteen queries. The trade is
        recorded in ADR-0003 and the flag stays off by default.
        """
        fused = fuse_by_rank([0, 5, 9], [9, 5, 0])
        assert math.isclose(fused[0], 1 / (RRF_K + 1) + 1 / (RRF_K + 3))
        assert math.isclose(fused[5], 2 / (RRF_K + 2))
        assert fused[0] > fused[5]

    def test_fused_scores_are_compressed_at_the_published_k(self) -> None:
        """The consequence of k=60 over a short head, asserted so nobody reads a fused score
        as a relevance strength: the best and worst nominated chunks differ by under a third
        of one per cent."""
        deep = list(range(CANDIDATE_DEPTH))
        fused = fuse_by_rank(deep, deep)
        best, worst = max(fused.values()), min(fused.values())
        assert (best - worst) / best < 0.35

    def test_empty_orderings_produce_no_scores(self) -> None:
        assert fuse_by_rank([], []) == {}


class TestCosineScores:
    def test_identical_unit_vectors_score_one(self) -> None:
        vector = [0.6, 0.8]
        assert math.isclose(cosine_scores(vector, [vector])[0], 1.0)

    def test_orthogonal_vectors_score_zero(self) -> None:
        assert math.isclose(cosine_scores([1.0, 0.0], [[0.0, 1.0]])[0], 0.0)

    def test_opposed_vectors_score_minus_one(self) -> None:
        assert math.isclose(cosine_scores([1.0, 0.0], [[-1.0, 0.0]])[0], -1.0)

    def test_every_row_is_scored_in_order(self) -> None:
        scores = cosine_scores([1.0, 0.0], [[1.0, 0.0], [0.0, 1.0], [-1.0, 0.0]])
        assert [round(score, 6) for score in scores] == [1.0, 0.0, -1.0]

    def test_a_dimension_mismatch_raises_rather_than_truncating(self) -> None:
        """A silently truncated dot product would be a plausible-looking wrong similarity."""
        try:
            cosine_scores([1.0, 0.0], [[1.0, 0.0, 0.0]])
        except ValueError:
            return
        raise AssertionError("expected a ValueError on mismatched dimensions")
