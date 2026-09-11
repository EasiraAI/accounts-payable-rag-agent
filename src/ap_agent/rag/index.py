"""Lexical index over the corpus.

## Why BM25 and not embeddings

The retrieval decision is argued in full in docs/adr/0003-retrieval-strategy.md. The short
version: queries in this domain are dominated by exact terminology that appears verbatim in
the policy text. "three-way match tolerance", "delegated authority", "goods receipt",
"FIN-POL-003" are not paraphrases of the corpus, they are quotations from it. BM25 scores
exactly that kind of overlap, and it does so deterministically, which means the grounding
tests run in continuous integration with no model, no network and no flaky ranking.

Dense retrieval earns its place when queries and documents share meaning but not words.
That is a real scenario, and it is why a hybrid mode exists behind a flag. It is not the
default, because adding a 100 MB model download to the install step to improve recall on
paraphrases the corpus does not contain would be paying a cost for a benefit we cannot
demonstrate.

What actually handles the corpus traps is neither of these. No ranking function knows that
version 1.0 of the authority matrix was superseded on 1 August 2026. That is metadata, and
it is applied in the retriever.

## Persistence format

The index is stored as JSON, not a pickle. A pickle is executable content, and loading one
from disk at startup means the retrieval path deserialises arbitrary code from a file that
an attacker who reached the filesystem could replace. BM25 statistics for a corpus this size
are rebuilt from the stored chunks in a few milliseconds, so the safer format costs
effectively nothing.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Final

from rank_bm25 import BM25Okapi

from ap_agent.domain.errors import IndexNotBuilt
from ap_agent.rag.dense import DenseEncoder
from ap_agent.rag.ingest import Chunk, IngestedCorpus, ingest_corpus

#: Tokens shorter than this carry no retrieval signal and inflate the vocabulary.
MIN_TOKEN_LENGTH: Final = 2

#: Words so frequent in policy prose that they separate nothing. Deliberately short: an
#: aggressive stop list would remove "must" and "not", and "must not" is the difference
#: between a permission and a prohibition in this corpus.
STOP_WORDS: Final[frozenset[str]] = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "for",
        "is",
        "are",
        "be",
        "been",
        "was",
        "were",
        "as",
        "at",
        "by",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "with",
        "from",
        "which",
        "when",
        "than",
        "then",
        "there",
        "their",
    }
)

_TOKEN = re.compile(r"[a-z0-9][a-z0-9\-.]*")

#: Irregular plurals and domain word forms that a suffix rule would get wrong.
_LEMMA_OVERRIDES: Final[dict[str, str]] = {
    "policies": "policy",
    "authorities": "authority",
    "tolerances": "tolerance",
    "discrepancies": "discrepancy",
    "duties": "duty",
    "currencies": "currency",
    "matching": "match",
    "matches": "match",
    "matched": "match",
    "approvals": "approval",
    "approves": "approve",
    "approved": "approve",
    "approver": "approve",
    "approvers": "approve",
    "receipts": "receipt",
    "receipted": "receipt",
    "invoices": "invoice",
    "invoiced": "invoice",
    "payments": "payment",
    "payments.": "payment",
    "vendors": "vendor",
    "duplicates": "duplicate",
    "changes": "change",
    "changed": "change",
    "limits": "limit",
    "thresholds": "threshold",
    "exceptions": "exception",
    "delegations": "delegation",
    "delegated": "delegate",
    "accruals": "accrual",
    "records": "record",
    "recorded": "record",
    "logs": "log",
    "logging": "log",
    "masked": "mask",
    "masking": "mask",
}


def _lemma(token: str) -> str:
    """Collapse a token to a crude lemma.

    A full stemmer (Porter, Snowball) was not used on purpose. The corpus vocabulary is
    small and the failure modes of aggressive stemming are worse here than the recall it
    buys: Porter maps "authority" and "authorise" to different stems while collapsing
    "policy" and "police". An explicit override table plus a conservative plural rule is
    auditable, and every entry in it can be justified against a query we actually expect.
    """
    if token in _LEMMA_OVERRIDES:
        return _LEMMA_OVERRIDES[token]
    if len(token) > 4 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
        return token[:-1]
    return token


def _is_identifier(token: str) -> bool:
    """Whether a compound token is an identifier rather than a hyphenated English phrase.

    The test is the presence of a digit. "fin-pol-003" and "adv-001" are identifiers;
    "purchase-order", "three-way" and "month-end" are ordinary compounds.
    """
    return any(character.isdigit() for character in token)


def tokenize(text: str) -> list[str]:
    """Tokenize for indexing and querying.

    Compound handling differs by kind, and the distinction matters more than it looks.

    **Identifiers** are emitted whole *and* split: "FIN-POL-003" yields ``fin-pol-003``,
    ``fin``, ``pol`` and ``003``. Keeping the whole form means an exact identifier match
    scores highest, and the parts let a query that writes the reference differently still
    match.

    **Ordinary hyphenated compounds are emitted as parts only**, without the whole form.
    Emitting both inflates term frequency for the compound: "purchase-order" would
    contribute three tokens where a plain-text "purchase order" contributes two, so a
    document that merely mentions a purchase order in passing outranks one that is entirely
    about the subject being asked. That was measured on this corpus, not assumed. Query Q09
    of the golden set ("approval needed for an emergency purchase with no purchase order")
    ranked a records-retention section above the emergency-purchase policy for exactly this
    reason: the retention section contains one hyphenated "purchase-order" worth three
    tokens, while the emergency policy's seven occurrences of "emergency" were worth one
    each. Splitting without the whole form removes the inflation and leaves the
    discriminative term deciding the ranking.
    """
    tokens: list[str] = []
    for raw in _TOKEN.findall(text.lower()):
        cleaned = raw.strip(".-")
        if len(cleaned) < MIN_TOKEN_LENGTH or cleaned in STOP_WORDS:
            continue
        is_compound = "-" in cleaned or "." in cleaned
        if not is_compound:
            tokens.append(_lemma(cleaned))
            continue
        if _is_identifier(cleaned):
            tokens.append(_lemma(cleaned))
        for part in re.split(r"[-.]", cleaned):
            if len(part) >= MIN_TOKEN_LENGTH and part not in STOP_WORDS:
                tokens.append(_lemma(part))
    return tokens


class CorpusIndex:
    """A BM25 index over section chunks.

    BM25 parameters are the library defaults (k1=1.5, b=0.75). They are not tuned, and
    tuning them on fifteen documents would fit the golden set rather than the task. The
    README records this as a known limitation.
    """

    _INDEX_FILENAME = "corpus_index.json"
    # Version 2 adds the optional embedding block. The load path refuses a mismatch and the
    # ingest command rebuilds, so an index written by an older build is replaced rather than
    # read with the wrong shape.
    _FORMAT_VERSION = 2

    def __init__(
        self,
        corpus: IngestedCorpus,
        *,
        embeddings: list[list[float]] | None = None,
        embedding_model: str | None = None,
    ) -> None:
        self._corpus = corpus
        self._chunks = corpus.chunks
        if not self._chunks:
            raise ValueError("cannot build an index over an empty corpus")
        if embeddings is not None and len(embeddings) != len(self._chunks):
            raise ValueError(
                f"index has {len(self._chunks)} chunks but {len(embeddings)} embeddings; "
                "re-run ingest"
            )
        self._tokenized = [tokenize(chunk.text) for chunk in self._chunks]
        self._bm25 = BM25Okapi(self._tokenized)
        self._embeddings = embeddings
        self._embedding_model = embedding_model

    # ---- properties ------------------------------------------------------------------

    @property
    def chunks(self) -> list[Chunk]:
        return self._chunks

    @property
    def corpus_hash(self) -> str:
        return self._corpus.corpus_hash

    @property
    def document_count(self) -> int:
        return self._corpus.document_count

    @property
    def has_embeddings(self) -> bool:
        return self._embeddings is not None

    @property
    def embedding_model(self) -> str | None:
        return self._embedding_model

    @property
    def embeddings(self) -> list[list[float]]:
        """The document matrix, in chunk order.

        Raises rather than returning an empty list when absent: an empty matrix would make
        every similarity zero and turn a missing-embedding bug into a silently lexical-only
        ranking, which is the exact failure this whole change exists to correct.
        """
        if self._embeddings is None:
            raise IndexNotBuilt(
                "this index carries no embeddings; re-run `ap-agent ingest --force` with "
                "AP_RETRIEVAL_MODE=hybrid"
            )
        return self._embeddings

    def __len__(self) -> int:
        return len(self._chunks)

    # ---- search ----------------------------------------------------------------------

    def score(self, query: str) -> list[tuple[Chunk, float]]:
        """Raw lexical scores for every chunk, unranked and unfiltered.

        Returning all of them, rather than a truncated top-k, is what lets the retriever
        apply metadata re-ranking before truncation. Truncating first would make demotion
        impossible: a superseded document could occupy a slot that a current one then could
        not reclaim.
        """
        tokens = self.query_terms(query)
        if not tokens:
            return [(chunk, 0.0) for chunk in self._chunks]
        scores = self._bm25.get_scores(tokens)
        return [(chunk, float(score)) for chunk, score in zip(self._chunks, scores, strict=True)]

    @staticmethod
    def query_terms(query: str) -> list[str]:
        """Distinct query terms, in first-appearance order.

        BM25 as published scores over the *set* of query terms, with any weighting for a
        term repeated in the query carried by a separate query-term-frequency factor that
        ``rank_bm25`` does not implement. Passing a token list with duplicates therefore adds
        that term's contribution twice, which is an implementation artefact rather than
        intended weighting.

        It is not a harmless one. Golden query Q09, "approval needed for an emergency
        purchase with no purchase order", repeats "purchase". Counting it twice promoted a
        records-retention section that happens to list document types above the
        emergency-purchase policy the question was about. Deduplicating moved Hit@3 from 0.94
        to 1.00 and MRR from 0.919 to 0.969 across the golden set, with no query regressing.

        Public because the orchestrator records the terms it searched on in the retrieval
        event: an auditor reconstructing a run needs to see what was actually queried, not
        only the natural-language string that produced it.

        Order is preserved rather than sorted so that a logged query term list reads the way
        the query was written.
        """
        seen: set[str] = set()
        terms: list[str] = []
        for token in tokenize(query):
            if token not in seen:
                seen.add(token)
                terms.append(token)
        return terms

    # ---- persistence -----------------------------------------------------------------

    def save(self, index_dir: Path) -> Path:
        index_dir.mkdir(parents=True, exist_ok=True)
        path = index_dir / self._INDEX_FILENAME
        payload: dict[str, object] = {
            "format_version": self._FORMAT_VERSION,
            "corpus_hash": self._corpus.corpus_hash,
            "document_count": self._corpus.document_count,
            "source_dir": self._corpus.source_dir,
            "chunks": [chunk.model_dump(mode="json") for chunk in self._chunks],
        }
        if self._embeddings is not None:
            # Rounded to six places, which is well inside the precision that matters for a
            # cosine similarity and keeps the file readable. Still JSON, not a pickle: an
            # index file travels between environments, and a pickle is executable content.
            payload["embedding_model"] = self._embedding_model
            payload["embeddings"] = [
                [round(value, 6) for value in vector] for vector in self._embeddings
            ]
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, index_dir: Path) -> CorpusIndex:
        path = index_dir / cls._INDEX_FILENAME
        if not path.is_file():
            raise IndexNotBuilt(str(index_dir))
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("format_version") != cls._FORMAT_VERSION:
            raise IndexNotBuilt(
                f"{index_dir} (index format version "
                f"{payload.get('format_version')}, expected {cls._FORMAT_VERSION}; re-run ingest)"
            )
        corpus = IngestedCorpus(
            chunks=[Chunk.model_validate(item) for item in payload["chunks"]],
            corpus_hash=payload["corpus_hash"],
            document_count=payload["document_count"],
            source_dir=payload["source_dir"],
        )
        raw = payload.get("embeddings")
        embeddings = (
            [[float(value) for value in vector] for vector in raw] if raw is not None else None
        )
        return cls(
            corpus,
            embeddings=embeddings,
            embedding_model=payload.get("embedding_model"),
        )


def build_index(
    corpus_dir: Path,
    index_dir: Path,
    *,
    force: bool = False,
    encoder: DenseEncoder | None = None,
) -> tuple[CorpusIndex, bool]:
    """Ingest and index a corpus, skipping the work when nothing changed.

    Returns the index and whether it was rebuilt. Idempotence matters operationally: the
    ingest command is safe to run on every start, and re-running it does not silently produce
    a different index from the same source.

    ``encoder`` adds the dense side. An existing index is reused only if it also matches on
    embeddings: the same corpus indexed without them is *stale* for a hybrid deployment, and
    treating it as current is what would let the mode fall back to lexical ranking without
    saying so.
    """
    corpus = ingest_corpus(corpus_dir)
    if not force:
        try:
            existing = CorpusIndex.load(index_dir)
        except IndexNotBuilt:
            existing = None
        fresh = (
            existing is not None
            and existing.corpus_hash == corpus.corpus_hash
            and (encoder is None or existing.embedding_model == encoder.model_name)
        )
        if fresh and existing is not None:
            return existing, False
    embeddings = (
        encoder.encode([chunk.text for chunk in corpus.chunks]) if encoder is not None else None
    )
    index = CorpusIndex(
        corpus,
        embeddings=embeddings,
        embedding_model=encoder.model_name if encoder is not None else None,
    )
    index.save(index_dir)
    return index, True


def load_or_build_index(
    corpus_dir: Path, index_dir: Path, *, encoder: DenseEncoder | None = None
) -> CorpusIndex:
    """Load the index, building it first if it is absent or stale."""
    index, _ = build_index(corpus_dir, index_dir, encoder=encoder)
    return index
