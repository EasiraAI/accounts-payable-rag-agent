"""Optional dense retrieval, and the rank fusion that combines it with BM25.

This module exists because the lexical index has one measured weakness. On the golden set's
paraphrase queries, which ask the corpus's questions in a finance analyst's words rather than
its own, four of eight miss the answering section entirely. "Is there a deadline to get a
supplier bill into this month's books" shares almost no vocabulary with FIN-POL-011 §1, and no
amount of BM25 tuning fixes a term overlap of nearly zero.

Two design points are worth stating before the code.

**Fusion is by rank, not by score.** A BM25 score and a cosine similarity are not
commensurable: one is an unbounded sum of per-term weights, the other is bounded in [-1, 1],
and their distributions move independently with query length. Normalising them onto a common
scale requires choosing a normalisation, and every choice is a tuning parameter that would be
fitted to this golden set. Reciprocal rank fusion avoids the question entirely by using only
the ordering each retriever produces, which is why it is the standard answer (Cormack et al.,
2009) and why it needs no per-corpus calibration.

**Only the head of each list contributes.** Each retriever nominates its top ``candidate_depth``
chunks and nothing else. Without that cutoff every chunk in the corpus would receive some
reciprocal-rank contribution from the dense side, since cosine similarity is never exactly
zero, and the fused ranking would be padded with chunks that answer nothing. That padding
would be worse than a short result: a recommendation citing six sections looks better sourced
than one citing two, whether or not the extra four are relevant.

The encoder is imported lazily. The default configuration never loads it, so the base install
carries no PyTorch, and a deployment that does not want a 100 MB model download does not get
one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Protocol

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

#: The default sentence-embedding model. Small, English, and good enough at this corpus size;
#: a finance-tuned model (voyage-finance-2, Fin-E5) only earns its place once the corpus is
#: large enough for the general model's failures to be measurable, which at 58 chunks it is
#: not. Named here rather than in orchestration, like every other model identifier.
DEFAULT_DENSE_MODEL: Final = "BAAI/bge-small-en-v1.5"

#: The constant in the reciprocal rank fusion denominator. 60 is the value Cormack et al.
#: published and the value every major implementation uses. It is deliberately not tuned: the
#: point of rank fusion here is to avoid introducing a parameter fitted to sixteen queries.
RRF_K: Final = 60

#: How many chunks each retriever nominates. Six results are returned, so a depth of twenty
#: lets a chunk that one retriever ranks poorly still be rescued by the other, without
#: admitting the long tail. See the module docstring on why the cutoff exists at all.
CANDIDATE_DEPTH: Final = 20


class SupportsEncode(Protocol):
    """The one method this package needs from a sentence-embedding model.

    A protocol rather than the concrete class so this module type-checks with the optional
    dependency absent, and so a test can substitute a stub encoder without a model download.
    The declared return is the shape this package consumes; the real implementation returns a
    numpy array, which satisfies it structurally at the point of use.
    """

    def encode(self, sentences: list[str], **kwargs: object) -> Sequence[Sequence[float]]: ...


class DenseEncoderUnavailable(RuntimeError):
    """Raised when dense retrieval is requested and its dependency is not installed."""

    def __init__(self, detail: str) -> None:
        super().__init__(
            f"dense retrieval is unavailable: {detail}. Install it with "
            "`uv sync --extra hybrid`, then re-run `ap-agent ingest --force` so the index "
            "carries embeddings."
        )


class DenseEncoder:
    """Wraps a sentence-embedding model behind the two calls this package makes.

    Separate from the index so the index stays a pure data structure that can be built, saved
    and loaded with no optional dependency present, and so the fusion below can be tested
    without loading a model at all.
    """

    def __init__(self, model_name: str = DEFAULT_DENSE_MODEL) -> None:
        self._model_name = model_name
        self._model: SupportsEncode | None = None

    @property
    def model_name(self) -> str:
        return self._model_name

    def _load(self) -> SupportsEncode:
        """Load the model on first use.

        Lazy because constructing a ``DenseEncoder`` happens during composition, which runs
        for every command including ones that never retrieve anything.
        """
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as error:  # pragma: no cover - exercised by the install matrix
                raise DenseEncoderUnavailable(str(error)) from error
            self._model = SentenceTransformer(self._model_name)
        return self._model

    def encode(self, texts: Sequence[str], *, is_query: bool = False) -> list[list[float]]:
        """Embed texts, normalised to unit length so a dot product is a cosine similarity.

        ``is_query`` prepends the instruction prefix that BGE models are trained to expect on
        the query side. Skipping it costs measurable retrieval quality on exactly the
        paraphrase cases this module exists for, and it applies to queries only: the documents
        are embedded bare.
        """
        prepared = (
            [f"Represent this sentence for searching relevant passages: {text}" for text in texts]
            if is_query
            else list(texts)
        )
        model = self._load()
        vectors = model.encode(prepared, normalize_embeddings=True, show_progress_bar=False)
        return [[float(value) for value in vector] for vector in vectors]


def cosine_scores(query_vector: Sequence[float], matrix: Sequence[Sequence[float]]) -> list[float]:
    """Similarity of one query vector against every document vector.

    A plain dot product, because both sides are unit-normalised at encode time. Written out
    rather than pulled from numpy so this module imports nothing heavy: the whole point of the
    lazy encoder is that the base install stays free of PyTorch.
    """
    return [sum(q * d for q, d in zip(query_vector, row, strict=True)) for row in matrix]


def fuse_by_rank(
    lexical_order: Sequence[int],
    dense_order: Sequence[int],
    *,
    candidate_depth: int = CANDIDATE_DEPTH,
    k: int = RRF_K,
) -> dict[int, float]:
    """Reciprocal rank fusion over two orderings of the same chunk set.

    Both arguments are chunk indices, best first. The result maps a chunk index to its fused
    score for every chunk that either retriever nominated, and omits the rest: a chunk absent
    from both heads contributed nothing and must not be padded into the result.

    Pure, integer-indexed, and free of any model, so the fusion behaviour is unit-testable
    without a 100 MB download. That matters more than it sounds: fusion is where a ranking bug
    would hide, and a test that needs a model download is a test that gets skipped.
    """
    fused: dict[int, float] = {}
    for order in (lexical_order, dense_order):
        for rank, index in enumerate(order[:candidate_depth], start=1):
            fused[index] = fused.get(index, 0.0) + 1.0 / (k + rank)
    return fused
