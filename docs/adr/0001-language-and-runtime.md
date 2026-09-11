# ADR-0001: Language and runtime

**Status:** Accepted. **Date:** 2026-09-11.

## Context

The brief permits Python or TypeScript and any environment. The system needs typed
contracts, decimal arithmetic, a small HTTP API, a CLI, local persistence, a lexical or
hybrid retriever, and a provider-agnostic LLM adapter. It must run on a reviewer's laptop
with a single install step.

## Options considered

| Option | Strengths | Weaknesses |
|---|---|---|
| **Python 3.12 + pydantic v2 + FastAPI + SQLite** | `decimal.Decimal` in the standard library; pydantic gives runtime-validated schemas that double as tool JSON schemas; richest RAG ecosystem (`rank_bm25`, `sentence-transformers`); FastAPI gives OpenAPI for free; SQLite needs no service. | GIL limits in-process concurrency (irrelevant at this scale); packaging is less uniform than npm. |
| TypeScript + zod + Fastify + better-sqlite3 | zod schemas are excellent; single toolchain; strong async model. | No native decimal type (needs `decimal.js`, and every arithmetic path must remember to use it); thinner retrieval library ecosystem; sentence-transformer embeddings need a sidecar or ONNX. |
| Python + Django | Batteries included, admin UI. | Far heavier than needed; no UI is required. |

## Decision

Python 3.12, pydantic v2, FastAPI, Typer for the CLI, SQLite via the standard library
`sqlite3` module, `uv` for locked dependencies.

## Rationale

The deciding factor is FIN-POL-002 §5: tolerance arithmetic must be decimal, in code, with
inputs, formula, result and rounding recorded. Python's `Decimal` makes float leakage a
type error at the schema boundary (pydantic `condecimal`), which is a stronger guarantee
than a convention. Pydantic models also serve three roles at once: tool argument
validation, LLM structured-output validation and API response schemas, so there is one
source of truth for every contract.

## Consequences

- Dependencies locked with `uv lock`; `pyproject.toml` is the manifest.
- Async FastAPI handlers wrap a synchronous orchestrator; concurrency is per-run via
  SQLite transactions, not threads.
- Windows, macOS and Linux all supported; no compiled extensions required in the default
  (BM25) retrieval mode.
