# Implementation plan: AP Processing and RAG Workflow Agent

Date: 2026-09-11. Timebox: 8 working hours. Spec: `docs/specs/2026-09-11-ap-agent-design.md`.

Priority order follows CLAUDE.md §10: bounded loop and deterministic reconciliation,
approval gate and idempotent decision tool, RAG with citations and adversarial doc, five
fixtures passing, persistence and resume, docs and diagram.

## Working practice, and the review gate on each phase

**Status: historical.** This is the plan as written before implementation, kept because the
reasoning behind the sequencing is worth more than a tidy record. Where the delivered system
differs — it has five tables rather than four, twelve rule modules rather than six, and a
second-signature gate this plan does not mention — the delivered state is described in
CLAUDE.md §15 and in the README.

Four practices apply throughout, and each exists because of a specific failure it prevents.

**A failing test before the code.** Every rule, tool and repository method starts with a test
that fails for the right reason. On a system whose value is in its thresholds, a test written
afterwards tends to assert what the code does rather than what the policy says.

**Boundary values, not representative values.** A tolerance engine is wrong at its edges or
nowhere. Each threshold gets a test just inside it, one just outside, and one exactly on it,
because "the lower of AUD 50 or 1%" has a different answer on either side of the crossover.

**Property-based tests where the invariant is stronger than any example.** Tolerance
arithmetic, reference normalisation and fingerprinting have invariants that hold for all
inputs, and a generator finds the case a person would not have thought to write.

**No claim without output.** A phase closes on pasted command output, not on a judgement that
it looks done.

Each phase then has a review gate, stated as a question rather than a checklist, because the
question is what survives when the code changes:

| Phase | The question the gate asks |
|---|---|
| 1. Contracts | Can an invalid state be represented? Does any schema field let a caller widen a permission? |
| 2. Rule engine | Does every threshold trace to a policy section, and does every citation say what the section actually says? |
| 3. Retrieval | Does the right section rank first, is the superseded document demoted, and does the distractor stay out? |
| 4. Tools and adapters | Does a read tool ever raise instead of reporting? Can the write tool act without an approval? |
| 5. Orchestration | Is every state transition reachable and every terminal state actually terminal? |
| 6. Fixtures | Does each case pass for the reason the case exists, or for an unrelated one? |
| 7. Safety | Assume the model is hostile and the corpus is planted: what can they cause? |
| 8. Reliability | What happens on a crash between any two writes? |
| 9. Delivery | Does every number in the documentation reproduce from a command? |

The gates are deliberately adversarial rather than confirmatory. A reviewer asked "is this
right?" will agree; asked "what would make this wrong?" they find the amount-mutation path at
the approval gate, which is how that one was found.

## Phase 0: Project bootstrap (0:00 to 0:30)

Tasks
1. `pyproject.toml` with `uv`; dependencies pinned: pydantic, pydantic-settings, fastapi,
   uvicorn, typer, rank_bm25, python-frontmatter, anthropic, httpx, structlog or stdlib
   logging; dev: pytest, pytest-asyncio, hypothesis, ruff, mypy. `uv lock`.
2. `src/ap_agent/__init__.py`, `config/settings.py` reading `.env`.
3. `scripts/` with `ingest.sh|ps1`, `serve`, `eval`; `infra/Dockerfile`, `infra/compose.yaml`.
4. `git init`, first commit, conventional messages from here on.
5. Remove `__MACOSX/` and `.DS_Store`, which are archive-extraction artefacts, and exclude
   both in `.gitignore`.

Exit: `uv run python -c "import ap_agent"` works; `pytest` collects zero tests and exits 0.

## Phase 1: Domain contracts (0:30 to 1:15)

Review gate: see the table above.

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

Exit: `pytest tests/contract/test_models.py` green; the contracts review finds no
blocking findings.

## Phase 2: Rule engine (1:15 to 2:30)

Review gate: see the table above.

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

Review gate: see the table above.

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

Review gate: see the table above.

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

Exit: golden tests green; the retrieval measurement reports Hit@1 >= 0.8 on the golden set and zero
ADV-002 leaks.

## Phase 5: Persistence, orchestrator, approval gate (4:30 to 6:00)

Review gate: see the table above.

Tasks
1. `persistence/repository.py` with the tables in ADR-0004, WAL mode, `BEGIN IMMEDIATE`
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

Review gate: see the table above.

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

Review gate: see the table above.

Tasks: run the eval tier with `--provider anthropic`; capture two transcripts (FIN-001
success, FIN-003 exception) into `docs/samples/`; record token counts for the cost note.
If no key, keep the fake-provider transcripts and label them.

## Phase 8: Review gate (7:00 to 7:20)

Five independent reviews, each asked one adversarial question and none allowed to see the
others' findings first: assume the model is hostile and the corpus is planted, what can they
cause; does every threshold and citation match the policy text; where does untrusted input
reach a decision; what fails silently; and do the tests actually verify the behaviour they
name. Fix blockers only, then a simplification pass over `src/` that changes no behaviour.

Exit: no "succeeded" attack vector; no swallowed exception around tool or model calls.

## Phase 9: Documentation and artefacts (7:20 to 8:00)

Review gate: see the table above. Every number in the documentation has to reproduce from a
command, and CLAUDE.md §11 and §15 have to match the delivered state rather than the plan.

Deliverables
1. `README.md`: environment choice (local), prerequisites, env vars, model config,
   ingestion, start, test/eval commands, supported flows, assumptions, known limitations,
   real/mocked/requires-external-access table per tool, cost and cleanup note (no cloud
   resources).
2. `docs/DESIGN_NOTE.md`: orchestration, RAG design, trust boundaries, model and tool
   contracts, persistence, failure handling, evaluation, production changes, enterprise
   scale.
3. `docs/diagrams/architecture.md` (Mermaid) and `docs/diagrams/run-timeline.md`.
4. `docs/MANIFEST.md`: model, runtime, store/index, persistence, tools, API surface,
   trust boundaries, each with the config key that controls it.
5. `docs/samples/`: success transcript, exception transcript, eval report.
6. `docs/RECOMMENDATIONS.md`: what to improve next (from the limitations list).
7. `docs/references.md` already drafted; confirm every entry is cited.
8. `docs/AI_USAGE_DECLARATION.md`: written last, the only place that discusses authoring tools.
9. Update `CLAUDE.md` §11 onward if any convention changed.

Exit: every number in the documentation reproduced from a command; the deliverables
checklist in CLAUDE.md §7 complete; the credential sweep clean.

## Risk register

| Risk | Mitigation |
|---|---|
| Timebox overrun on RAG hybrid mode | Hybrid is a flag; ship BM25 only if needed and say so |
| Windows path and SQLite locking quirks | WAL mode plus `timeout=5` on connect; tests run on Windows first |
| Live model returns schema-invalid output | Repair path plus `FakeClient` for all required assertions |
| Fixture data drift from corpus policy | A policy-conformance review after Phases 2 and 6, checking each citation against the corpus text |
| Documentation drifts from the code | Every stated number reproduces from a command; a documentation review before delivery cross-checks each claim against the source |

## Definition of done

All items in CLAUDE.md §7 checked; five fixtures PASS with pasted output; the credential
sweep clean; no unmasked bank data in tracked files; the dependency lock file present; every
number in the documentation reproducible from a command.
