# ADR-0003: Retrieval strategy

**Status:** Accepted. **Date:** 2026-09-11.

## Context

The corpus is fifteen short Markdown policy documents with YAML front matter carrying
`document_id`, `version`, `status` (current, superseded, untrusted), `effective_date` and
tags. Three documents are traps: a superseded authority matrix, an adversarial supplier
notice and an irrelevant travel policy. Queries are short and dominated by domain terms
("three-way match tolerance", "delegated authority limit", "duplicate invoice").

The brief requires ranked chunks with document ID, type, version or page, relevance score
and citation metadata, plus an explicit statement of strategy and limitations.

## Options considered

| Option | Fit for this corpus | Determinism | External dependency | Notes |
|---|---|---|---|---|
| **BM25 over section chunks with metadata re-ranking (default)** | High: domain vocabulary is exact and shared between queries and policy text. | Fully deterministic; unit-testable without a model. | None. | Robertson and Zaragoza (2009). |
| Dense embeddings only (hosted, e.g. Voyage or OpenAI) | Good recall for paraphrase; weaker on exact identifiers like `FIN-POL-003`. | Deterministic per model version, but a network call in tests. | API key and network. | Adds a second provider to configure. |
| Local dense embeddings (`sentence-transformers`, e.g. `bge-small`) | Good; no network. | Deterministic. | 100 MB+ model download, torch dependency. | Heavy for a laptop install step. |
| **Hybrid BM25 + dense with reciprocal rank fusion (optional mode)** | Best recall in the literature for mixed queries. | Deterministic given both rankers. | Same as chosen dense option. | Cormack et al. (2009) RRF. |
| Vector database (Chroma, Qdrant, pgvector) | Unnecessary at 15 documents and 58 chunks. | n/a | Service or extra package. | Named as the production change. |

## Decision

1. **Chunking:** one chunk per Markdown `##` section, inheriting the document front matter.
   Section boundaries are the natural citation unit in policy text ("FIN-POL-002 §2"),
   chunks stay under about 300 tokens, and no sentence is split.
2. **Index:** BM25 (`rank_bm25`, BM25Okapi) over lowercased, stemmed-lite tokens, persisted
   as JSON with a corpus hash so re-ingestion is skipped when unchanged.
3. **Metadata-aware re-ranking, applied after lexical scoring:**
   - `status: superseded` multiplied by 0.3 and labelled `superseded` in the citation.
   - `status: untrusted` keeps its score but is labelled `untrusted` and routed to the
     risk assessor, never to the policy synthesiser.
   - A query-time filter `doc_types=[policy]` excludes non-policy documents from the
     policy retrieval step; the evidence step retrieves without the filter so ADV-001 can
     be found and flagged.
4. **Optional hybrid mode** (`AP_RETRIEVAL_MODE=hybrid`): adds local `bge-small-en-v1.5`
   embeddings and fuses with RRF (k=60). Off by default to keep install light.
5. **Output contract:** every chunk returns `chunk_id`, `document_id`, `title`, `version`,
   `status`, `section`, `score`, `rank`, `text`, and a `citation` string.

## Rationale

For a small, well-structured, terminology-dense corpus, lexical retrieval with metadata
is not a compromise; it is the accurate choice. Research on hybrid retrieval shows dense
methods help most on paraphrased or long natural-language queries, which are not the
query pattern here. Determinism matters more: retrieval grounding tests must run in CI
without a model or network. Metadata re-ranking is what actually handles the traps; no
embedding model knows that version 1.0 is superseded.

### A correction worth recording

An earlier draft of this record said the index was persisted as a pickle. It is JSON, and the
difference is a safety property rather than a preference: a pickle is executable content, so
loading one is equivalent to running whatever wrote it. An index file in a data directory is
exactly the sort of artefact that gets copied between environments, and a retrieval index is
not worth that exposure. The corpus hash beside it is what makes a stale index detectable, and
nothing about the format needs to be.

## Known limitations (to be repeated in the design note)

- No semantic matching in the default mode: a query phrased entirely in synonyms can miss.
- BM25 parameters are defaults (k1=1.5, b=0.75), not tuned; the corpus is too small to tune
  without overfitting.
- Permissions filtering (FIN-POL-010 §3) is modelled as a metadata filter on
  `classification`; there is no identity provider.
- Re-ranking weights (0.3 for superseded) are policy choices, not learned values.
- The index is rebuilt on ingestion; there is no incremental update or deletion
  propagation, which FIN-POL-010 §5 would require in production.
