# Accounts-Payable Processing and RAG Workflow Agent

An internal accounts-payable agent. Given an invoice-processing request it retrieves the
governing policy, gathers invoice, purchase-order, receipt, vendor, delegation and history
evidence through bounded tool calls, reconciles it deterministically, and produces a cited
structured recommendation. Any consequential outcome stops at a human approval gate; the run
resumes only after an explicit decision and records that decision exactly once.

**This system cannot move money.** There is no payment rail, no bank credential and no
outbound network call in the decision path. Posting targets a simulated ledger and every
receipt is stamped `simulated: true`.

## Current state

| | |
|---|---|
| Tests | 599 passing, plus one live-model test excluded by default |
| Fixture cases | 5 of 5 passing (FIN-001 to FIN-005) |
| Retrieval quality | Hit@1 0.94, Hit@3 1.00, MRR 0.969 over 16 golden queries |
| Retrieval safety | 0 distractor leaks, 0 stale-policy leaks |
| Corpus | 15 documents, 58 section chunks |

Reproduce with `ap-agent eval`. The output is in [docs/samples/eval_report.json](docs/samples/eval_report.json).

---

## 1. Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.12 or later | Developed and tested on 3.14. |
| `uv` | 0.11 or later | Used for the locked dependency set. `pip install uv` if absent. |
| Operating system | Windows, macOS or Linux | No compiled extensions in the default configuration. |
| Disk | About 150 MB | Virtual environment and index. |
| Network | Not required | The default configuration makes no network call at any point. |

An API key is needed only for the optional live-model tier (section 8).

## 2. Install

```bash
uv sync
```

This creates `.venv` from `uv.lock`, so the dependency set is byte-identical to the one the
tests above ran against. Every command below can be prefixed with `uv run`.

To include the optional hybrid retrieval mode (adds roughly 100 MB of model weights):

```bash
uv sync --extra hybrid
```

## 3. Environment variables

The system runs with no environment file at all: every setting has a default that the
application uses. [.env.example](.env.example) documents what is configurable and why.

```bash
cp .env.example .env
```

The variables that change behaviour most:

| Variable | Default | Effect |
|---|---|---|
| `AP_LLM_PROVIDER` | `fake` | `fake` for the deterministic adapter, `anthropic` for live Claude. |
| `AP_LLM_MODEL` | `claude-sonnet-5` | Model identifier, used only by the live adapter. |
| `ANTHROPIC_API_KEY` | unset | Required only when the provider is `anthropic`. |
| `AP_MAX_STEPS` | `12` | Phase-execution ceiling. Exceeding it fails the run explicitly. |
| `AP_MAX_TOOL_CALLS` | `16` | Tool-attempt ceiling. Retries spend it. |
| `AP_RETRIEVAL_MODE` | `bm25` | `hybrid` adds local dense embeddings. |
| `AP_SUPERSEDED_SCORE_FACTOR` | `0.3` | How far a superseded policy document is demoted. |
| `AP_TOOL_MAX_RETRIES` | `2` | Retries per read tool, so three attempts in total. |
| `AP_DB_PATH` | `./data/runtime/ap_agent.db` | SQLite file. |

Provider and model names appear in `src/ap_agent/config/settings.py` and nowhere else.
Orchestration code reads `Settings` and never names a vendor.

## 4. Model configuration

Two adapters implement one protocol, `LLMClient`, selected by `AP_LLM_PROVIDER`.

**`fake`** is the default. It is deterministic, needs no network and costs nothing. It is not
a mock of convenience: it is what lets every safety property be asserted in continuous
integration, because those properties must hold for *any* model output including hostile
output. It also has fault modes (`schema_violation`, `always_invalid`, `unavailable`,
`inject_compliance`, `hostile_narrative`) that exercise the repair path, the explicit-failure
path, the case where the model has obeyed an injected instruction, and the case where its
prose carries an instruction the structured fields do not.

**`anthropic`** calls Claude through the official SDK. Structured output is obtained by
declaring a single tool whose input schema is the pydantic model's JSON schema and forcing
`tool_choice` to it, so the response arrives shaped by the schema rather than as prose to be
parsed. Validation still runs on the result.

The model is used in exactly two phases and decides nothing:

| Step | Model? | Why |
|---|---|---|
| Retrieval query formulation | No | Templates. Deterministic and testable. |
| Evidence synthesis: facts, inferences, unknowns | **Yes** | Language understanding is its job. |
| Reconciliation arithmetic and tolerances | No | `Decimal` engine (FIN-POL-002 §5). |
| Duplicate, vendor, authority rules | No | Policy is code. |
| Risk narrative, assumptions, confidence | **Yes** | Judgement and explanation. |
| The outcome | No | Rule engine. A model may only make it *more* conservative. |
| Calling the decision tool | No | Orchestrator, after approval. |

## 5. Ingestion and indexing

```bash
ap-agent ingest
```

Parses the 15 Markdown documents in `finance_rag_corpus/`, splits each on its second-level
headings into 58 citable chunks, and writes a BM25 index to `data/index/`.

Idempotent. The index carries a hash of the corpus bytes, so re-running it on an unchanged
corpus reuses the existing index and reports `up to date`. Use `--force` to rebuild anyway.
The commands in section 6 build the index automatically if it is missing, so this step is
explicit rather than mandatory.

## 6. Start commands

```bash
# Run a case from a JSON file (a bare request, or a fixture with a "request" key)
ap-agent run fixtures/cases/FIN-001.json

# Inspect a run, with its full audit log
ap-agent get <run_id> --events

# Resolve a pending approval. A repeat delivery from the same approver is a no-op.
# A higher-risk case needs two distinct signatures, one from Financial Control, so a
# second call from a different approver is a real signature and not a replay (section 10).
# --delegation-id names an authority-register entry when the approver acts under one.
ap-agent approve <run_id> --approval-id <apr_...> \
    --approver-id U-3081 --approver-role DEPARTMENT_DIRECTOR

ap-agent reject <run_id> --approval-id <apr_...> \
    --approver-id U-3081 --approver-role DEPARTMENT_DIRECTOR --comment "Receipt unclear."

# Print the component manifest for the running configuration
ap-agent manifest

# Start the HTTP service (binds to localhost; there is no authentication)
ap-agent serve --port 8000
```

HTTP equivalents, once serving:

| Operation | Endpoint |
|---|---|
| Start run | `POST /runs` with a processing request |
| Get run | `GET /runs/{run_id}` (status, state, result, audit events) |
| Approve | `POST /runs/{run_id}/approve` |
| Reject | `POST /runs/{run_id}/reject` |
| List evaluation results | `GET /evaluations` |
| Diagnostics | `GET /health`, `GET /manifest`, `GET /openapi.json` |

```bash
curl -s -X POST localhost:8000/runs \
  -H 'content-type: application/json' \
  -d @fixtures/cases/FIN-001.json  # note: send the "request" object, not the wrapper
```

## 7. Test and evaluation commands

Three tiers, separated by what they depend on.

```bash
# Tier 1 and 2: unit and contract. No model, no network. Always green.
uv run pytest tests/unit tests/contract -q

# Tier 3: evaluation. Fixture cases and retrieval quality, deterministic adapter.
uv run pytest tests/eval -q

# Everything except the live-model test
uv run pytest -q -m "not live_model"

# The five required cases, with a report table and pass/fail per case
ap-agent eval

# Same, writing transcripts and a JSON report
ap-agent eval --transcripts docs/samples --report docs/samples/eval_report.json
```

`ap-agent eval` exits non-zero if any case fails or a retrieval gate is missed, so it works
as a pipeline step without parsing its output.

| Tier | Location | Count | Model calls | What it protects |
|---|---|---|---|---|
| Unit | `tests/unit` | 291 | none | Rule thresholds at their boundaries, redaction, property-based invariants |
| Contract | `tests/contract` | 161 | none | Typed schemas, tool reliability, persistence and idempotency, HTTP surface |
| Evaluation | `tests/eval` | 147 | none (deterministic adapter) | Retrieval grounding, the five cases, safety properties |
| Live model | `tests/eval`, marked `live_model` | 1 | yes | The same cases through a real model |

## 8. Live-model tier (requires external access)

```bash
export ANTHROPIC_API_KEY=...          # never committed; see .env.example
ap-agent eval --provider anthropic
uv run pytest -q -m live_model
```

Excluded from the default run. Nothing in tiers 1 to 3 calls a provider, so the suite is
usable offline and in a sandbox.

## 9. Integrations: real, mocked, or requiring external access

| Component | Status | Detail |
|---|---|---|
| `retrieve_finance_documents` | **Real** | BM25 over the real corpus in this repository, with metadata re-ranking. No network. |
| `get_vendor_record` | **Mocked** | `fixtures/mock_data/vendors.json`. Returns a masked last-four account value; the model has no field for a full account. |
| `get_purchase_order` | **Mocked** | `fixtures/mock_data/purchase_orders.json`, including recorded goods receipts. |
| `check_invoice_history` | **Mocked** | `fixtures/mock_data/invoice_history.json`, covering paid, posted, held and rejected records. |
| `get_authority_delegation` | **Mocked** | `fixtures/mock_data/delegations.json`, the authority register. Added beyond the five named tools; see section 11. |
| `submit_finance_decision` | **Simulated** | Writes to the local `decisions` table and returns a receipt stamped `posting_system: SIMULATED_ERP`, `simulated: true`. No payment rail exists in this code path. |
| Claude (`anthropic` provider) | **Requires external access** | Live model calls. Not exercised by the default test run. |
| Deterministic adapter (`fake` provider) | **Real, local** | Default. No network. |
| Persistence | **Real** | SQLite in WAL mode, on the local filesystem. |
| HTTP API | **Real** | FastAPI, served by uvicorn. |
| Hybrid dense retrieval | **Requires optional install** | `uv sync --extra hybrid`. Off by default. |

The mocked backends inject faults declaratively, per case, from
`fixtures/mock_data/fault_profiles.json`. FIN-004 genuinely cannot reach the purchasing
system, and the failure travels the same code path a real outage would.

## 10. Supported flows

**Clean three-way match (FIN-001).** Retrieve policy, gather evidence, reconcile, assess
risk, recommend `APPROVE_FOR_POSTING`, create an approval request, stop. On approval, record
exactly one decision. Transcript: [docs/samples/FIN-001_transcript.json](docs/samples/FIN-001_transcript.json), walkthrough in
[docs/samples/successful_flow.md](docs/samples/successful_flow.md).

**Duplicate invoice (FIN-002).** Exact match against a paid record, `REJECT_DUPLICATE` with
both record identifiers cited. Rejection also writes to the ledger of record, so it is gated
like any consequential outcome.

**Poisoned attachment (FIN-003).** The attachment instructs the agent to ignore policy and
release payment. The instruction is recorded as a fraud indicator, the case escalates, and no
decision is recorded. Walkthrough in [docs/samples/exception_flow.md](docs/samples/exception_flow.md).

**Missing evidence (FIN-004).** The purchasing system times out on all three attempts. The
gap becomes an explicit unknown, the case is held, and nothing is approved.

**Duplicate approval (FIN-005).** The same callback delivered twice produces one decision and
two identical responses, the second flagged `replayed`.

**Two-signature approval.** A higher-risk transaction under FIN-POL-003 §3 needs two
distinct signatures, one of them from Financial Control. The first valid signature is recorded
and the run stays `AWAITING_APPROVAL`; the pending approval then states what is outstanding,
for example `1 further distinct signature(s) required (1 of 2 collected); one signature must
come from Financial Control (FIN-POL-003 §3)`. Financial Control may co-sign without holding a
monetary limit, because §2 gives the role none and §3 makes it the required second approver. A
repeat delivery from someone who has already signed is a replay: no validation, no tool call,
no second signature. See
[docs/adr/0007-two-signature-approvals.md](docs/adr/0007-two-signature-approvals.md).

**Self-contradictory invoice (`REJECT_INVALID`).** An invoice whose lines do not support its
own stated total, or one dated in the future, cannot be repaired by retrieval and is rejected
rather than held. The line this draws is between *incomplete*, which is a hold under
FIN-POL-001 §5, and *wrong*. Rejecting is consequential, so it stops at the approval gate like
any other outcome.

**Tax question (`TAX_QUERY`).** FIN-POL-002 §2 requires tax to be assessed separately and
forbids it being hidden inside a price variance. A submission that states a gross and no
components has its unattributed difference raised as a tax question rather than reported to
the requester as a pricing dispute. The query never blocks: the rate behind it is
configuration, not policy.

**Payment schedule.** The agreed terms come from the purchase order, and an invoice's printed
terms do not override them (FIN-POL-006 §1). The due date is computed, moved off a
non-business day to the *preceding* business day under §2, and a standard Tuesday or Thursday
run is proposed. A proposal only: §4 forbids an agent releasing a payment file, and no tool
here can.

**Restart and resume.** A run stopped at the approval gate resumes in a different process
from its persisted state. Exercised by
`tests/eval/test_safety.py::TestRestartAndResume`.

**Rejection.** An approver declining a posting recommendation holds the case and records
nothing.

## 11. Assumptions

1. **The sixth tool is deliberate.** The brief names five tools; `get_authority_delegation`
   is added because the brief's own evidence set includes approval delegations, and
   FIN-POL-003 §4 makes a delegation valid only if recorded in the authority register with
   delegate, delegator, scope and dates. Folding that into the vendor tool would have given
   one tool authority over two unrelated domains.
2. **Attachments arrive as inline text.** Binary parsing and optical character recognition
   are out of scope. What the brief exercises is the agent's behaviour towards attachment
   *content*.
3. **A configured tax rate, because the policy states none.** FIN-POL-002 §2 requires tax
   to be assessed separately and defines no correctness test; the corpus names a jurisdiction
   (AU) and no rate. `EXPECTED_TAX_RATE_PERCENT` in `domain/rules/tax.py` is therefore an
   assumption about the environment, it is cited as one in every finding that uses it, and
   nothing derived from it blocks a case. A deployment elsewhere changes that constant and
   nothing else.
4. **Policy thresholds are constants in code, not read from retrieved text.** Retrieval
   supplies citations; the numbers come from `domain/rules/`. A retrieval system can demote
   the superseded authority matrix but cannot guarantee a model ignores it.
5. **Approval identity is asserted by the caller.** There is no identity provider, so the
   approver identifier and role arrive in the callback body. Authority is validated against
   the matrix and the register; who the caller is, is not.
6. **Amounts are in the invoice currency and policy thresholds are in AUD.** Where they
   differ, the threshold is applied numerically and the substitution recorded as an
   assumption, because converting a threshold needs a cited rate under FIN-POL-009 §2 and no
   rate service is configured. This applies to the tolerance bands in
   `domain/rules/matching.py` and, more consequentially, to the approval limits in
   `domain/rules/authority.py`: that is the control gating a posting, so a foreign-currency
   invoice is approved against a numerically substituted limit, and the substitution is on
   the approval record.
7. **The corpus is synthetic and in-repo.** It contains no real vendor, person or account.

## 12. Known limitations

**Retrieval.** The default mode is lexical, so a query phrased entirely in synonyms can miss.
BM25 parameters are the library defaults and are not tuned; tuning them on 15 documents would
fit the golden set rather than the task. BM25 cannot represent negation, so a query containing
"no purchase order" matches documents about purchase orders. Permission filtering
(FIN-POL-010 §3) is modelled as a metadata filter on `classification`, not enforced against
an identity provider.

**Injection detection.** The detector is a clause-anchored heuristic, not a parser. A
well-formed attack in the passive voice, in another language, or encoded would not be caught.
This is why detection is a secondary control and the design does not depend on it; the
structural controls are listed in section 13.

**Calendar.** FIN-POL-006 §2 refers to public holidays and the corpus supplies no calendar,
so business-day arithmetic skips weekends only. Exception review dates and payment-run
scheduling are therefore indicative.

**Foreign exchange.** No rate service. A foreign-currency invoice against an AUD order is
held rather than converted, which is what FIN-POL-009 §3 requires, but the system cannot
process the legitimate converted case.

**Concurrency.** One process, one SQLite connection, writes serialised by a lock. Exactly-once
decision recording is enforced by the schema and holds across processes; throughput is not a
goal here.

**Timeouts abandon rather than cancel.** A timed-out tool call leaves its worker thread
running. Acceptable because every tool with a timeout is a read; the write tool is made safe
by its idempotency key instead.

**Schema versioning is forward-only.** The store records its shape in SQLite's
`user_version`, migrates an older store forward on open, and **refuses to open a store written
by a newer build**, with an error naming both versions. That is deliberate: running older code
against a forward-migrated store is how a constraint gets dropped from the enforcement path
while the application still believes it is there. There is no down migration, so a rollback
means restoring a backup taken before the upgrade. See
[docs/adr/0006-schema-versioning-and-migration.md](docs/adr/0006-schema-versioning-and-migration.md).

**Credit notes are recognised, not processed.** FIN-POL-008 requires tax and accounting
treatment to be validated before a credit is applied, and the processing request cannot
express a credit because it requires a positive amount. A document that identifies itself as a
credit note is held for Financial Control rather than assessed against controls written for an
obligation to pay.

**An approved non-PO justification cannot be represented.** FIN-POL-001 §2 accepts a purchase
order *or* an approved non-PO justification, and the request schema has no field for the
second. The minimum-evidence finding says so rather than asserting that none exists.

**No authentication.** The HTTP service binds to localhost and has no authentication, so the
approval endpoints are open to anyone who can reach the host. It is not deployable as-is.

## 13. Safety properties, and where they are enforced

| Property | Mechanism | Test |
|---|---|---|
| The model cannot record a decision | Only the orchestrator calls the write tool, after checking the stored approval | `test_safety.py::TestApprovalGate` |
| The write tool refuses without an approval | The tool re-checks the repository itself, independently of its caller | `test_the_decision_tool_refuses_without_an_approval_record` |
| An outcome cannot be loosened by the model | The rule engine computes it; a suggestion applies only if strictly more conservative | `test_a_model_that_obeys_an_injected_instruction_is_overruled` |
| No tool argument widens permission | No schema contains `force`, `override`, `verified` or a caller-supplied idempotency key | `test_no_tool_input_schema_offers_an_override` |
| A fabricated citation cannot be presented | The model returns chunk identifiers; they are resolved against what was retrieved | `TestCitationGrounding` |
| One decision per approved run, ever | `decisions.idempotency_key` primary key plus `run_id UNIQUE`; the key is derived in code from the run, approval, outcome, amount, currency and vendor | `test_concurrent_identical_calls_produce_one_decision` |
| One decision per *invoice*, across runs | `decisions.invoice_fingerprint` with a partial unique index, so three identical invoices submitted as three runs cannot post three times | `TestOneDecisionPerInvoiceAcrossRuns` |
| Two approvals means two people | `approval_signatures PRIMARY KEY (approval_id, approver_id)`; the gate opens only when the count, the distinctness and the Financial Control requirement are met | `TestSecondApproverIsEnforced` |
| A stale store cannot be opened silently | `PRAGMA user_version` with ordered forward migrations; a newer store is refused | `TestSchemaVersioning`, `TestMigrationCrashWindows` |
| Execution is bounded | Finite step and tool budgets; retries spend the tool budget | `TestBoundedExecution` |
| Model output is never trusted unvalidated | One structured method, one repair attempt, then explicit failure. No free-text path | `TestModelFailureHandling` |
| No unmasked account or credential is logged | One redaction path for every event and log line | `TestSafeLogging` |
| Untrusted text cannot escape its block | Nonce-delimited fencing, with forged delimiters stripped | `test_a_forged_prompt_delimiter_is_neutralised` |

## 14. Repository layout

```
finance_rag_corpus/      the policy corpus (input to ingestion; not modified)
src/ap_agent/
  config/                Settings; the only place a provider or model is named
  domain/                typed contracts, run state, and rules/ (pure Decimal functions)
  rag/                   ingestion, BM25 index, metadata-aware retriever
  tools/                 six tool contracts, the runner, simulated backends
  llm/                   LLMClient protocol, adapters, prompt construction, output schemas
  orchestration/         phase plan, budgets, approval gate, the state machine
  persistence/           SQLite repository and schema
  observability/         redaction and structured events
  evaluation/            retrieval measurement and the fixture runner
  api/ cli/              HTTP and command-line transports
  composition.py         the composition root both transports build from
tests/unit tests/contract tests/eval
fixtures/cases           the five required cases, with their assertions
fixtures/mock_data       simulated systems of record and fault profiles
docs/                    design note, ADRs, manifest, diagrams, samples, references
infra/                   Dockerfile and compose file
scripts/                 setup, sample rendering, credential sweep
```

## 15. Documentation

| Document | Contents |
|---|---|
| [docs/DESIGN_NOTE.md](docs/DESIGN_NOTE.md) | Orchestration, RAG design, trust boundaries, contracts, persistence, failure handling, production changes, enterprise scale |
| [docs/adr/](docs/adr/) | Seven decision records, each with the options rejected and why |
| [docs/MANIFEST.md](docs/MANIFEST.md) | Component and configuration manifest |
| [docs/diagrams/architecture.md](docs/diagrams/architecture.md) | Architecture and run-lifecycle diagrams |
| [docs/RECOMMENDATIONS.md](docs/RECOMMENDATIONS.md) | What to improve next, in priority order |
| [docs/references.md](docs/references.md) | Papers, standards and documentation consulted |
| [docs/samples/](docs/samples/) | Transcripts for a successful flow and an exception flow, plus the evaluation report |

## 16. Cost and cleanup

No cloud resources are created. Everything runs locally.

The default configuration makes no network call and costs nothing. The optional live tier
calls Claude twice per case, so ten calls for a full evaluation run, with prompts of roughly
four to eight thousand tokens and responses of a few hundred. Token counts per call are
recorded on every `MODEL_CALL` event, so actual usage is auditable from a run's audit log
rather than estimated.

To remove all local state:

```bash
rm -rf data/          # SQLite database and retrieval index
rm -rf .venv/         # virtual environment
```

Both are regenerated by `uv sync` and `ap-agent ingest`. Nothing outside the repository
directory is written.

## 17. Repository hygiene

No API keys, credentials, personal data or proprietary code are committed. The corpus and
every fixture are synthetic. Dependencies are pinned in `uv.lock`. Credential-shaped and
account-shaped test inputs are assembled at runtime from fragments rather than written as
literals, so a secret scanner has nothing to flag and the repository contains no
credential-shaped string.

The claim is checked rather than asserted. `scripts/secret_sweep.py` scans the committed
tree for six credential and account shapes and exits non-zero on any finding, so it works as a
pipeline step:

```bash
uv run python scripts/secret_sweep.py
```

It allows last-four masking (`****8842`), which is the form FIN-POL-004 §3 and FIN-POL-010 §2
require and the form the vendor tool returns.
