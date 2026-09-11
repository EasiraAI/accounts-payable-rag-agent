# Implementation plan: AP Processing and RAG Workflow Agent

Date: 2026-09-11. Timebox: 8 working hours. Spec: `docs/superpowers/specs/2026-09-11-ap-agent-design.md`.

Priority order follows CLAUDE.md §10: bounded loop and deterministic reconciliation,
approval gate and idempotent decision tool, RAG with citations and adversarial doc, five
fixtures passing, persistence and resume, docs and diagram.

## How the toolchain is used

Every phase names the skills, subagents and plugins that apply. Process skills set the
approach; project skills carry domain facts; subagents review; plugins verify.

| Resource | Type | Used for |
|---|---|---|
| `superpowers:test-driven-development` / `mattpocock-skills:tdd` | process | every engine, tool and repository module: failing test first |
| `superpowers:subagent-driven-development` | process | run independent phase tasks in parallel where they share no state |
| `superpowers:verification-before-completion` | process | no PASS claim without pasted output |
| `superpowers:systematic-debugging` / `mattpocock-skills:diagnosing-bugs` | process | any failing fixture |
| `mattpocock-skills:domain-modeling` / `codebase-design` | process | Phase 1 contracts and package boundaries |
| `ap-policy-rules` (project) | knowledge | rule engine thresholds and citations |
| `fixture-cases` (project) | knowledge | fixture data and assertions |
| `trust-boundaries` (project) | knowledge | prompts, tool schemas, decision tool, logging |
| `run-evals` (project) | procedure | test commands and report interpretation |
| `engineering-voice` (project) | style | all docs |
| `ap-controls-reviewer` (project agent) | review | after Phase 3 and Phase 6 |
| `contracts-reviewer` (project agent) | review | after Phase 1 |
| `rag-evaluator` (project agent) | review | after Phase 4 |
| `safety-red-team` (project agent) | review | after Phase 5 and before delivery |
| `delivery-editor` (project agent) | review | Phase 9 |
| `security-reviewer`, `silent-failure-hunter`, `pr-test-analyzer`, `code-reviewer` (dotclaude / official plugins) | review | Phase 8 gate |
| `test-writer` (dotclaude) | generation | property tests for tolerance arithmetic |
| `context7` | docs lookup | pydantic v2, FastAPI, Anthropic SDK, rank_bm25 current APIs |
| `claude-api` skill | reference | model IDs, structured output via tool use, pricing for the cost note |
| `artifact-diagramming` / `dataviz` | diagram | architecture diagram and run timeline in Phase 9 |
| `claude-md-management:revise-claude-md` | maintenance | keep CLAUDE.md §11+ accurate at the end |
| `code-review` / `simplify` | quality | final pass on `src/` |
| hooks (`secret_guard`, `lint_python`, `stop_checklist`) | enforcement | continuous |

Not used, and why: `vercel:*`, `supabase:*`, `n8n`, `monday`, `zapier` (cloud services
would break the single-command local run and add cost); `frontend-design`, `impeccable`,
`ui-ux-pro-max`, `stitch` (no UI required); `claude-seo`, `claude-ads` (irrelevant);
GitHub MCP (repository is delivered as an archive; git is used locally for history only).

## Phase 0: Project bootstrap (0:00 to 0:30)

Tasks
1. `pyproject.toml` with `uv`; dependencies pinned: pydantic, pydantic-settings, fastapi,
   uvicorn, typer, rank_bm25, python-frontmatter, anthropic, httpx, structlog or stdlib
   logging; dev: pytest, pytest-asyncio, hypothesis, ruff, mypy. `uv lock`.
2. `src/ap_agent/__init__.py`, `config/settings.py` reading `.env`.
3. `scripts/` with `ingest.sh|ps1`, `serve`, `eval`; `infra/Dockerfile`, `infra/compose.yaml`.
4. `git init`, first commit, conventional messages from here on.
5. Remove `__MACOSX/` and `.DS_Store` (zip artefacts) after confirming with the owner.

Exit: `uv run python -c "import ap_agent"` works; `pytest` collects zero tests and exits 0.

## Phase 1: Domain contracts (0:30 to 1:15)

Skills: `mattpocock-skills:domain-modeling`, `trust-boundaries`, `contracts-reviewer` agent.

Tasks
1. Enums: `Outcome`, `ExceptionCategory`, `VendorStatus`, `RunPhase`, `RunStatus`,
   `TrustLevel`, `ApprovalStatus`.
2. Money: `Money(amount: Decimal, currency: str)` with quantize helpers; reject floats.
3. Evidence models: `Invoice`, `InvoiceLine`, `PurchaseOrder`, `POLine`, `GoodsReceipt`,
   `VendorRecord` (bank details as `masked_account_last4` only in outward models),
   `InvoiceHistoryMatch`, `RetrievedChunk`, `Citation`.
4. Output models: `Calculation`, `ExceptionRecord`, `SourcedFact`, `Inference`,
   `Unknown`, `PolicyFinding`, `ActionRecord`, `Recommendation`, `FinalResult`.
5. `ProcessingRequest` with `UntrustedText` wrapper for `notes` and `attachments`.
6. Contract tests: JSON round-trip; float rejection; twelve required fields present.

Exit: `pytest tests/contract/test_models.py` green; `contracts-reviewer` report has no
blocking findings.

## Phase 2: Rule engine (1:15 to 2:30)

Skills: `ap-policy-rules`, TDD, `test-writer` for Hypothesis strategies, `ap-controls-reviewer`.

Tasks
1. `three_way_match(invoice, po, receipts) -> MatchResult` with per-line `Calculation`
   objects: goods `min(50, 1% of line)`, services `min(100, 2%)`, freight rule, quantity
   rule, currency rule, missing-receipt rule. Decimal, `ROUND_HALF_UP`, currency recorded.
2. `duplicate_check(invoice, history) -> DuplicateResult` exact and fuzzy per FIN-POL-005 §1.
3. `vendor_status_check(vendor) -> list[ExceptionRecord]`, plus 30-day and bank-change flags.
4. `authority_check(total_aud, approver_role, delegation) -> AuthorityResult` using the v4.0
   matrix and two-approver triggers.
5. `fraud_indicators(evidence_texts, vendor, request) -> list[Indicator]` with the phrase
   list for injection language.
6. `decide_outcome(match, dup, vendor, authority, indicators) -> Outcome` with precedence:
   REJECT_DUPLICATE > ESCALATE (indicators >= 2 or sanctions) > HOLD (any exception) >
   APPROVE_FOR_POSTING.
7. Property tests: variance within tolerance never produces a PRICE_VARIANCE; splitting an
   amount across lines never changes the total outcome; rounding stable.

Exit: unit tier green; every rule function has a `policy_ref` in its result.

## Phase 3: Tools with mocks and fault injection (2:30 to 3:30)

Skills: `trust-boundaries`, TDD, `silent-failure-hunter` after.

Tasks
1. `tools/base.py`: `ToolSpec` (name, purpose, permission, timeout, retries), `run_tool()`
   wrapper that enforces timeout with a thread executor, retries transient errors with
   backoff, emits `TOOL_CALL` events with duration and outcome, and increments the budget.
2. Mock backends in `fixtures/mock_data/*.json`: vendors, POs with receipts, invoice
   history, keyed by fixture case. A `FaultProfile` per case (`timeout_always`,
   `fail_then_succeed`).
3. `get_vendor_record`, `get_purchase_order`, `check_invoice_history` (mocked; labelled).
4. `retrieve_finance_documents` (real, over the local index; Phase 4 supplies the index).
5. `submit_finance_decision` (simulated posting adapter): requires APPROVED approval in the
   repository, validates arguments, idempotency via the `decisions` table (Phase 5
   supplies the table; stub interface now).

Exit: contract tests for each tool's Input/Output; FIN-004 fault profile produces exactly
`MAX_RETRIES + 1` attempts in events.

## Phase 4: RAG pipeline (3:30 to 4:30)

Skills: `context7` for `rank_bm25` and front-matter parsing, `rag-evaluator` after.

Tasks
1. `rag/ingest.py`: parse front matter; split on `## ` headings; chunk carries
   `document_id`, `title`, `version`, `status`, `effective_date`, `classification`,
   `section`, `chunk_id = f"{document_id}#{n}"`.
2. `rag/index.py`: BM25Okapi; persist with corpus hash; `hybrid` mode behind a flag with
   lazy import so the default install has no torch.
3. `rag/retriever.py`: query -> scored chunks -> metadata re-rank (superseded x0.3,
   untrusted labelled) -> optional `doc_types` filter -> top-k with `rank`, `score`,
   `citation`.
4. `tests/eval/retrieval_golden.yaml` and `test_retrieval_grounding.py`: Hit@1/Hit@3/MRR,
   distractor and stale-leak checks.
5. CLI `ingest` command; idempotent rebuild.

Exit: golden tests green; `rag-evaluator` reports Hit@1 >= 0.8 on the golden set and zero
ADV-002 leaks.

## Phase 5: Persistence, orchestrator, approval gate (4:30 to 6:00)

Skills: `trust-boundaries`, `ap-policy-rules`, TDD, `safety-red-team` after.

Tasks
1. `persistence/repository.py` with the four tables (ADR-0004), WAL mode, `BEGIN IMMEDIATE`
   for decisions, optimistic `version` on runs.
2. `observability/redact.py` and `events.py`; JSON lines log; unit tests asserting a full
   account number never survives redaction.
3. `llm/prompts.py` fencing function; `llm/fake_client.py` with canned per-phase outputs and
   fault modes; `llm/anthropic_client.py` with forced tool-use structured output and one
   repair attempt.
4. `orchestration/machine.py`: driver loop, phase functions from the spec §4, budgets in
   `gates.py`, approval creation and stop, resume entry point.
5. `EXECUTE_DECISION` path: idempotency key computed in code; `submit_finance_decision`
   invoked once; `APPROVAL_REPLAYED` on duplicates.
6. Tests: restart/resume with a fresh repository connection; concurrent double approve via
   two threads yields one decision row.

Exit: FIN-001 and FIN-005 pass end to end with `FakeClient`.

## Phase 6: API, CLI, fixtures, eval runner (6:00 to 6:45)

Skills: `fixture-cases`, `run-evals`, `context7` for FastAPI.

Tasks
1. FastAPI routes: `POST /runs`, `GET /runs/{id}`, `POST /runs/{id}/approve`,
   `POST /runs/{id}/reject`, `GET /evaluations`. Approve/reject body carries `approval_id`,
   `approver_id`, `approver_role`, `decision`. Responses are the typed models.
2. Typer CLI mirroring the routes plus `ingest`, `eval`, `serve`.
3. `fixtures/cases/FIN-00{1..5}.json` and the eval runner producing the table and
   `docs/samples/eval_report.json` plus per-case transcripts.
4. Run all five; fix with `systematic-debugging` until green.

Exit: `python -m ap_agent.cli eval --provider fake` prints five PASS rows; `GET
/evaluations` returns the same.

## Phase 7: Live model pass (6:45 to 7:00, optional if a key is available)

Skills: `claude-api`.

Tasks: run the eval tier with `--provider anthropic`; capture two transcripts (FIN-001
success, FIN-003 exception) into `docs/samples/`; record token counts for the cost note.
If no key, keep the fake-provider transcripts and label them.

## Phase 8: Review gate (7:00 to 7:20)

Run in parallel: `safety-red-team`, `ap-controls-reviewer`, `security-reviewer`,
`silent-failure-hunter`, `pr-test-analyzer`. Fix blockers only. Then `simplify` on `src/`.

Exit: no "succeeded" attack vector; no swallowed exception around tool or model calls.

## Phase 9: Documentation and artefacts (7:20 to 8:00)

Skills: `engineering-voice`, `delivery-editor`, `artifact-diagramming`,
`claude-md-management:revise-claude-md`.

Deliverables
1. `README.md`: environment choice (local), prerequisites, env vars, model config,
   ingestion, start, test/eval commands, supported flows, assumptions, known limitations,
   real/mocked/requires-external-access table per tool, cost and cleanup note (no cloud
   resources).
2. `docs/DESIGN_NOTE.md` (1 to 2 pages) with the eleven sections from `engineering-voice`.
3. `docs/diagrams/architecture.md` (Mermaid) and `docs/diagrams/run-timeline.md`.
4. `docs/MANIFEST.md`: model, runtime, store/index, persistence, tools, API surface,
   trust boundaries, each with the config key that controls it.
5. `docs/samples/`: success transcript, exception transcript, eval report.
6. `docs/RECOMMENDATIONS.md`: what to improve next (from the limitations list).
7. `docs/references.md` already drafted; confirm every entry is cited.
8. `docs/AI_USAGE_DECLARATION.md`: written last, the only place that discusses authoring tools.
9. Update `CLAUDE.md` §11 onward if any convention changed.

Exit: `delivery-editor` checklist fully ticked; `stop_checklist` hook items all true.

## Risk register

| Risk | Mitigation |
|---|---|
| Timebox overrun on RAG hybrid mode | Hybrid is a flag; ship BM25 only if needed and say so |
| Windows path and SQLite locking quirks | WAL mode plus `timeout=5` on connect; tests run on Windows first |
| Live model returns schema-invalid output | Repair path plus `FakeClient` for all required assertions |
| Fixture data drift from corpus policy | `ap-controls-reviewer` after Phases 2 and 6 |
| Documentation reveals authoring process | `engineering-voice` skill plus `delivery-editor` sweep |

## Definition of done

All items in CLAUDE.md §7 checked; `stop_checklist` list true; five fixtures PASS with
pasted output; no secrets or unmasked bank data in tracked files; dependency lock file
present.
