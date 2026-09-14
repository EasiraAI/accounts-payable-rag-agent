# Design spec: Accounts-Payable Processing and RAG Workflow Agent

Date: 2026-09-11. **Status: historical** — this is the design as proposed, before
implementation. It is kept because the alternatives it weighs are the reason the delivered
shape is what it is. Where the two differ, the delivered state is described in the README and
in PROJECT_SPEC.md §15; ADR-0006 and ADR-0007 record the two decisions taken after this was written.
Decisions referenced: ADR-0001 to ADR-0005 in `docs/adr/`.

## 1. Goal

An internal AP agent that takes a processing request, retrieves the governing policy,
gathers invoice, PO, receipt, vendor and history evidence through bounded tool calls,
reconciles deterministically, and produces a cited structured recommendation. Any
consequential outcome stops at an approval gate; the run resumes only after an explicit
human decision and executes the decision exactly once. Every run is reproducible from
its persisted state and audit log.

Non-goals: a UI, real payment rails, real ERP integration, multi-tenant auth.

## 2. Architecture

```
            +-------------------+        +--------------------------+
 HTTP/CLI --| API layer (FastAPI|------->| Orchestrator (state      |
            |  + Typer CLI)     |        |  machine, budgets, gates)|
            +-------------------+        +-----+---------+----------+
                                               |         |
                       +-----------------------+         +----------------------+
                       v                                                        v
            +---------------------+   +----------------------+   +---------------------------+
            | Rule engine         |   | LLM adapter          |   | Tool layer (typed)        |
            | Decimal matching,   |   | Anthropic | Fake     |   | retrieve_finance_documents|
            | duplicates, vendor, |   | structured output    |   | get_vendor_record         |
            | authority, risk     |   | fenced prompts       |   | get_purchase_order        |
            +---------------------+   +----------------------+   | check_invoice_history     |
                                                                 | submit_finance_decision   |
                                                                 +-------------+-------------+
                                                                               |
            +---------------------+   +----------------------+   +-------------v-------------+
            | Repository (SQLite) |   | Event log (redacted) |   | RAG index (BM25 + meta)   |
            | runs, events,       |   | JSON lines + DB      |   | section chunks of corpus  |
            | approvals, decisions|   |                      |   |                           |
            +---------------------+   +----------------------+   +---------------------------+
```

Trust boundary: everything left of the tool layer is policy. Tool results, retrieved
chunks and case input are data. The boundary is drawn in §4 of the design note and enforced
in `llm/prompts.py`, `tools/contracts.py` and `observability/redact.py`.

## 3. Package layout (`src/ap_agent/`)

| Package | Responsibility | Depends on |
|---|---|---|
| `config/` | `Settings` (pydantic-settings) loaded from env; the only place provider and model names appear | none |
| `domain/` | Typed contracts: `ProcessingRequest`, `Invoice`, `PurchaseOrder`, `VendorRecord`, `Citation`, `Calculation`, `Exception`, `Recommendation`, `FinalResult`, enums | none |
| `domain/rules/` | Pure functions: `three_way_match`, `duplicate_check`, `vendor_status_check`, `authority_check`, `fraud_indicators`, `decide_outcome` | `domain` |
| `rag/` | `ingest.py` (front matter + section chunker), `index.py` (BM25, optional hybrid), `retriever.py` (metadata re-rank, filters, citation output) | `domain` |
| `tools/` | One module per tool with `Input`/`Output` models, timeout wrapper, retry policy, mock backends with fault injection | `domain`, `rag`, `persistence` |
| `llm/` | `LLMClient` protocol, Anthropic and Fake adapters, `prompts.py` with the single fencing function | `domain`, `config` |
| `orchestration/` | `phases.py`, `state.py`, `machine.py`, `gates.py` | all above |
| `persistence/` | `repository.py` (SQLite schema and typed methods), `migrations.sql` | `domain` |
| `observability/` | `events.py` (typed event emitter with duration), `redact.py`, JSON log formatter | `domain` |
| `api/` | FastAPI app: `POST /runs`, `GET /runs/{id}`, `POST /runs/{id}/approve`, `POST /runs/{id}/reject`, `GET /evaluations` | `orchestration`, `persistence` |
| `cli/` | Typer: `ingest`, `run`, `get`, `approve`, `reject`, `eval`, `serve` | same as API |

## 4. Run lifecycle

Phases and the code that owns each transition:

| Phase | Work | Model call | Tool calls |
|---|---|---|---|
| `INTAKE` | Validate `ProcessingRequest`; mark `notes`/`attachments` untrusted; compute case fingerprint | no | none |
| `RETRIEVE_POLICY` | Template queries (matching, authority, duplicates, vendor) -> retriever with `doc_types=[policy]` | no | retrieve x1..3 |
| `GATHER_EVIDENCE` | Vendor record, PO with receipts, invoice history; retrieval without policy filter over case text to surface supplier documents. Each call has timeout + bounded retry; failure recorded as an unknown, not an exception | no | 3..4 |
| `RECONCILE` | Rule engine over typed facts: match, duplicates, vendor status, authority, FX. Emits `Calculation[]` and `Exception[]` | no | none |
| `ASSESS_RISK` | Fraud indicators (FIN-POL-005 §3) including injection phrases from untrusted text; model synthesises sourced facts and inferences from fenced evidence | yes (structured) | none |
| `RECOMMEND` | Engine computes outcome; model writes assumptions, confidence, narrative; validator ensures model cannot loosen the outcome | yes (structured) | none |
| `AWAITING_APPROVAL` | If outcome is consequential (post, reject, hold with posting implication): create approval request, persist, stop. Otherwise skip to `COMPLETED` with `actions_taken=[]` | no | none |
| `EXECUTE_DECISION` | On APPROVED: compute idempotency key, call `submit_finance_decision`; on REJECTED: record and complete | no | 1 (idempotent) |
| `COMPLETED` / `HELD` / `FAILED` | Terminal. `FinalResult` persisted | no | none |

Budgets: `AP_MAX_STEPS` (default 12) counts phase executions including retries;
`AP_MAX_TOOL_CALLS` (default 10) counts tool invocations including retry attempts.
Exceeding either sets `FAILED` with reason `BUDGET_EXHAUSTED`.

Tool selection is by need, not by loop: the PO tool is called only when the request or
invoice carries a PO reference; history is always called (FIN-POL-005 §1); vendor is
always called (FIN-POL-001 §5). This is the deterministic answer to "call other tools only
when needed".

## 5. Contracts (summary)

`Recommendation`: `outcome`, `cited_evidence: list[Citation]`, `calculations:
list[Calculation]`, `assumptions: list[str]`, `confidence: Decimal 0..1 with basis`,
`exceptions: list[ExceptionRecord]`, `next_action`.

`FinalResult`: `sourced_facts: list[SourcedFact]`, `calculations`, `inferences:
list[Inference]`, `unknowns: list[Unknown]`, `policy_findings: list[PolicyFinding]`,
`actions_taken: list[ActionRecord]`, plus `recommendation`. Both objects live in the run
output; twelve fields total, none collapsed.

`Citation`: `document_id`, `title`, `version`, `status`, `section`, `chunk_id`, `quote`.

`Calculation`: `name`, `inputs: dict[str, Decimal|str]`, `formula`, `result`,
`rounding`, `currency`, `policy_ref`.

Tool contracts: each tool exports `Input`, `Output`, `PERMISSION` (read or write),
`TIMEOUT_S`, `MAX_RETRIES`. `submit_finance_decision.Input` carries `run_id`,
`approval_id`, `outcome`, `amount`, `currency`, `idempotency_key`; the tool raises unless
the repository shows that approval as APPROVED for that run.

## 6. Persistence and resume

Per ADR-0004. State is saved after every phase. `POST /runs/{id}/approve` loads the run,
verifies phase is `AWAITING_APPROVAL`, records the approval, and resumes the driver in the
same request. Resume after restart is the same code path, exercised by a test that opens
a fresh repository connection.

## 7. Failure handling

| Failure | Handling | Observable |
|---|---|---|
| Tool timeout | `concurrent.futures` timeout wrapper; retry with exponential backoff up to `MAX_RETRIES`; then record `Unknown(source=tool)` and continue | `TOOL_CALL` events with `outcome=timeout`, `attempt`, `duration_ms` |
| Transient tool error | Same policy; typed `TransientToolError` vs `PermanentToolError` | same |
| Malformed model output | One repair call with validation error; then `FAILED(MODEL_OUTPUT_INVALID)` | `MODEL_CALL` event with `validation_error` |
| Duplicate approval | Idempotency table; stored response returned with `replayed=true` | `APPROVAL_REPLAYED` event |
| Restart mid-run | State reloaded; phase re-entered; reads repeat, the only write is idempotent | `RUN_RESUMED` event |
| Budget exhausted | `FAILED(BUDGET_EXHAUSTED)` with partial `FinalResult` | `BUDGET_EXCEEDED` event |

## 8. Evaluation

`tests/unit`: rule engine (tolerance edge cases with Hypothesis), chunker, redaction,
idempotency key, fencing function.
`tests/contract`: tool schemas round-trip, API responses match models, repository
round-trip, restart/resume.
`tests/eval`: FIN-001..005 through the full driver with `FakeClient`; assertions from the
Fixture assertions live in the fixture files themselves, so the expected control behaviour
is declared once; `GET /evaluations` and `ap-agent eval` run this tier and emit a report.
Retrieval golden set: query -> expected `document_id §section`, Hit@1, Hit@3, MRR, plus
"no ADV-002 in top-k" and "FIN-POL-003 above FIN-POL-003-OLD" checks.
Live tier: same eval with `AnthropicClient`, labelled optional.

## 9. Production changes (design note will expand)

PostgreSQL and a durable-execution engine for the driver; vector store with permission
filtering and deletion propagation; real ERP and vendor-master adapters behind the same
tool contracts; OpenTelemetry export; secrets manager; per-tenant corpora; approver
identity via SSO with FIN-POL-003 §5 evidence fields; rate limiting and cost budgets per
run; a review queue UI.

## 10. Enterprise scale

Concurrency via one driver per run with optimistic version checks; horizontal workers
pull from a queue; corpus partitioned by legal entity and jurisdiction; FX via a rate
service with cited rate IDs; multi-currency tolerances expressed in policy currency;
audit retention seven years per FIN-POL-010 in an append-only store; policy versions
pinned per run so a later policy change does not rewrite history.

## 11. Open questions resolved by assumption

- Currency of fixtures: AUD, matching the corpus jurisdiction.
- "Approval" is a single approver with role and limit in the callback body; two-approver
  flows are modelled in the engine as a requirement but only one callback is needed to
  demonstrate the gate. Documented as a limitation.
- Attachments are inline text in the request; file upload is out of scope.
