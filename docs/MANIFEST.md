# Component and configuration manifest

Every row names the component, where it is configured, and where it is implemented. A live
version of the same information is served by `GET /manifest` and printed by
`ap-agent manifest`; that one is generated from the running configuration, so it cannot
describe a system other than the one answering the request.

Verified against commit state: 15 corpus documents, 58 chunks, 599 tests passing, 5 of 5
fixture cases passing.

---

## 1. Model

| Item | Value | Configured by | Implemented in |
|---|---|---|---|
| Adapter protocol | `LLMClient`, one method returning a validated pydantic model | n/a | `llm/base.py` |
| Default provider | `fake`, deterministic, no network | `AP_LLM_PROVIDER` | `llm/fake_client.py` |
| Live provider | Claude via the official SDK | `AP_LLM_PROVIDER=anthropic` | `llm/anthropic_client.py` |
| Model identifier | `claude-sonnet-5` | `AP_LLM_MODEL` | `config/settings.py` |
| Credential | read from the provider's conventional variable | `ANTHROPIC_API_KEY` | `config/settings.py` |
| Max output tokens | 2048 | `AP_LLM_MAX_TOKENS` | `config/settings.py` |
| Request timeout | 30s | `AP_LLM_TIMEOUT_SECONDS` | `config/settings.py` |
| Transport retries | 2, handled by the SDK | `AP_LLM_MAX_RETRIES` | `llm/anthropic_client.py` |
| Schema repair attempts | 1, then explicit failure | not configurable, deliberately | `llm/anthropic_client.py` |
| Structured output | forced tool use; the schema is the tool's input schema | n/a | `llm/anthropic_client.py` |
| Phases that call it | `ASSESS_RISK`, `RECOMMEND` | n/a | `orchestration/machine.py` |
| What it may decide | nothing; a suggestion applies only if strictly more conservative | n/a | `llm/schemas.py` |

Provider and model names appear in `config/settings.py` and nowhere else in the codebase.

## 2. Agent runtime

| Item | Value | Configured by | Implemented in |
|---|---|---|---|
| Pattern | explicit state machine, no agent framework | n/a | `orchestration/machine.py` |
| Phases | INTAKE, RETRIEVE_POLICY, GATHER_EVIDENCE, RECONCILE, ASSESS_RISK, RECOMMEND, AWAITING_APPROVAL, EXECUTE_DECISION | n/a | `orchestration/phases.py` |
| Terminal states | COMPLETED, HELD, FAILED | n/a | `domain/enums.py` |
| Step budget | 12 | `AP_MAX_STEPS` | `orchestration/gates.py` |
| Tool-attempt budget | 16, derived from the phase plan | `AP_MAX_TOOL_CALLS` | `orchestration/phases.py` |
| Retries spend budget | yes | n/a | `tools/base.py` |
| Reconciliation | pure Decimal functions, no I/O, no clock of their own | n/a | `domain/rules/` |
| Outcome precedence | ESCALATE > REJECT_DUPLICATE > REJECT_INVALID > HOLD > APPROVE | n/a | `domain/rules/outcome.py` |
| Approval gate | pure function of stored state; no argument asserts approval | n/a | `orchestration/gates.py` |
| Rejected alternatives | LangGraph, provider tool-loop, Temporal | n/a | `adr/0002` |

## 3. Document store and index

| Item | Value | Configured by | Implemented in |
|---|---|---|---|
| Corpus | 15 Markdown documents with YAML front matter | `AP_CORPUS_DIR` | `finance_rag_corpus/` |
| Chunking | one chunk per second-level heading, 58 total | n/a | `rag/ingest.py` |
| Citation unit | `FIN-POL-002 §2`, matching the corpus's own reference form | n/a | `rag/ingest.py` |
| Index | BM25Okapi over lemmatised tokens | `AP_RETRIEVAL_MODE=bm25` | `rag/index.py` |
| Persisted as | JSON, not a pickle | `AP_INDEX_DIR` | `rag/index.py` |
| Staleness detection | SHA-256 of corpus file names and bytes | n/a | `rag/ingest.py` |
| Optional hybrid mode | local dense embeddings fused by reciprocal rank | `AP_RETRIEVAL_MODE=hybrid` plus the `hybrid` extra | `rag/index.py` |
| Top-k | 6 | `AP_RETRIEVAL_TOP_K` | `config/settings.py` |
| Superseded demotion | score multiplied by 0.3 | `AP_SUPERSEDED_SCORE_FACTOR` | `rag/retriever.py` |
| Type filtering | policy lookups return `policy` only | n/a | `rag/retriever.py` |
| BM25 parameters | library defaults (k1=1.5, b=0.75), untuned | n/a | `rag/index.py` |
| Measured quality | Hit@1 0.94, Hit@3 1.00, MRR 0.969 over 16 queries | n/a | `tests/eval/retrieval_golden.json` |
| Gates | Hit@3 ≥ 0.90, MRR ≥ 0.88, zero leaks | n/a | `evaluation/retrieval.py` |

### Corpus traps and how each is handled

| Document | Trap | Mechanism |
|---|---|---|
| `FIN-POL-003-OLD` | superseded authority matrix, lexically near-identical to the current one | excluded from policy lookups by type; demoted by status elsewhere |
| `ADV-001` | supplier notice instructing the agent to skip controls and pay | excluded from policy lookups; retrievable as evidence, labelled untrusted, counted as an indicator when attached to a case |
| `ADV-002` | travel policy with plausible monetary limits, topically irrelevant | excluded from policy lookups by type; never cited |

## 4. Persistence

| Item | Value | Configured by | Implemented in |
|---|---|---|---|
| Engine | SQLite, WAL journal, `synchronous=FULL` | `AP_DB_PATH` | `persistence/schema.sql` |
| Tables | `runs`, `events`, `approvals`, `approval_signatures`, `decisions` | n/a | `persistence/schema.sql` |
| Run concurrency | optimistic, `runs.version` asserted on write | n/a | `persistence/repository.py` |
| Decision idempotency | `decisions.idempotency_key` PRIMARY KEY | n/a | `persistence/schema.sql` |
| One decision per run | `decisions.run_id` UNIQUE, for the run's lifetime | n/a | `persistence/schema.sql` |
| One decision per invoice | `decisions.invoice_fingerprint`, partial UNIQUE index, across runs | n/a | `persistence/schema.sql` |
| One signature per approver | `approval_signatures` PRIMARY KEY `(approval_id, approver_id)` | n/a | `persistence/schema.sql` |
| Key derivation | SHA-256 of run, approval, outcome, amount, currency and vendor, computed in code | n/a | `persistence/repository.py` |
| Invoice fingerprint | SHA-256 of vendor, normalised number, currency and gross amount | n/a | `persistence/repository.py` |
| Schema version | `SCHEMA_VERSION = 2`, recorded in `PRAGMA user_version` | n/a | `persistence/repository.py` |
| Migration | ordered forward steps, each with its version stamp in one transaction | n/a | `persistence/repository.py` |
| A newer store | refused to open, with an error naming both versions | n/a | `persistence/repository.py` |
| Caller-supplied keys | not accepted; there is no such field | n/a | `tools/contracts.py` |
| Write transactions | `BEGIN IMMEDIATE`, plus a process-local lock per instance | n/a | `persistence/repository.py` |
| Event log | append-only, `UNIQUE(run_id, sequence)` | n/a | `persistence/schema.sql` |
| Resume | reload state, re-enter the stored phase; no separate recovery path | n/a | `orchestration/machine.py` |
| SQL location | one module, nowhere else | n/a | `persistence/repository.py` |

## 5. Tools

Generated from the specs by `describe_tools`, so this table cannot drift from the code.

| Tool | Permission | Timeout | Attempts | Integration | Purpose |
|---|---|---|---:|---|---|
| `retrieve_finance_documents` | READ | 5.0s | 2 | **real** (local index) | Ranked, citable chunks with document identifier, version, status, section and score |
| `get_vendor_record` | READ | 2.0s | 3 | **mocked** | Vendor status, masked payment details, risk flags, last-updated timestamp |
| `get_purchase_order` | READ | 2.0s | 3 | **mocked** | Line items, totals, currency, approval status, recorded goods receipts |
| `check_invoice_history` | READ | 2.0s | 3 | **mocked** | Candidate prior records across paid, posted, held and rejected history |
| `get_authority_delegation` | READ | 2.0s | 3 | **mocked** | Authority register entry: delegate, delegator, scope, dates, revocation |
| `submit_finance_decision` | WRITE | 5.0s | 1 | **simulated** | Records a consequential outcome against `SIMULATED_ERP` |

### Tool guarantees

| Guarantee | How |
|---|---|
| Exactly one tool may write | asserted by test; the other five are READ |
| The write tool is deny-by-default | refuses unless the repository holds an APPROVED approval for that run and that outcome |
| The write tool is not retried | `max_retries=0`; safety comes from the idempotency key, not a retry loop |
| No argument widens permission | no schema contains `force`, `override`, `verified`, `skip_checks` or an idempotency key |
| No full bank account is modelled | `VendorRecord` has only `bank_account_last4` |
| Money cannot move | no payment rail, no bank credential, no outbound call in this path |
| Failures are classified | transient and timeout retried; permanent and schema-mismatch not |
| Faults are declarative | per case, in `fixtures/mock_data/fault_profiles.json` |

## 6. API surface

| Operation | Method and path | CLI equivalent | Status codes |
|---|---|---|---|
| Start run | `POST /runs` | `ap-agent run <file>` | 201, 422 |
| Get run | `GET /runs/{run_id}` | `ap-agent get <id> --events` | 200, 404 |
| List runs | `GET /runs?limit=` | n/a | 200 |
| Approve | `POST /runs/{run_id}/approve` | `ap-agent approve ...` | 200 (including replays), 409, 422 |
| Reject | `POST /runs/{run_id}/reject` | `ap-agent reject ...` | 200, 409, 422 |
| List evaluation results | `GET /evaluations` | `ap-agent eval` | 200 |
| Health | `GET /health` | n/a | 200 |
| Manifest | `GET /manifest` | `ap-agent manifest` | 200 |
| Schema | `GET /openapi.json` | n/a | 200 |
| Build index | n/a | `ap-agent ingest [--force]` | n/a |
| Serve | n/a | `ap-agent serve --port` | n/a |

Both transports are built from `composition.py`, so a change cannot apply to one and miss the
other. A replayed approval returns 200 with an identical body rather than 409, because a
duplicate delivery is a success in an at-least-once world and a conflict status would push a
well-behaved caller into retrying a normal event.

## 7. Trust boundaries

| Level | Contents | May |
|---|---|---|
| **Policy (trusted)** | system prompt, orchestration code, rule engine, tool permission table | decide, act |
| **Evidence (untrusted)** | retrieved chunks, vendor, order, history and register records | be quoted, cited, compared |
| **Case input (untrusted)** | request `notes`, `attachments`, invoice text | be quoted, cited, compared |

### Enforcement, and where each is implemented

| Control | Implemented in | Asserted by |
|---|---|---|
| The model cannot call the write tool | `orchestration/machine.py` | `test_safety.py::TestApprovalGate` |
| The write tool re-checks approval independently | `tools/contracts.py` | `test_the_decision_tool_refuses_without_an_approval_record` |
| The outcome is computed from typed facts | `domain/rules/outcome.py` | `tests/unit/test_outcome.py` |
| A model suggestion may only tighten | `orchestration/machine.py`, `llm/schemas.py` | `test_a_model_that_obeys_an_injected_instruction_is_overruled` |
| No tool argument widens permission | `tools/contracts.py` | `test_no_tool_input_schema_offers_an_override` |
| Untrusted text is fenced with a per-run nonce | `llm/prompts.py` | `test_a_forged_prompt_delimiter_is_neutralised` |
| Citations resolve against what was retrieved | `orchestration/machine.py` | `TestCitationGrounding` |
| Injection is an indicator, not an instruction | `domain/rules/fraud.py` | `test_the_injected_instruction_becomes_a_fraud_indicator` |
| Indicators come only from case content | `domain/rules/fraud.py` | `test_a_corpus_level_injection_does_not_contribute_indicators` |
| One redaction path for every event and log line | `observability/redact.py` | `TestSafeLogging`, `tests/unit/test_redact.py` |
| Execution is bounded | `orchestration/gates.py`, `tools/base.py` | `TestBoundedExecution` |
| Model output is never unvalidated | `llm/base.py` and the adapters | `TestModelFailureHandling` |
| Two approvals means two people | `persistence/schema.sql` (`approval_signatures` primary key), `domain/results.py` (`signature_requirement_met`), `orchestration/machine.py` | `TestSecondApproverIsEnforced` |
| Financial Control may co-sign without a limit | `domain/rules/authority.py` (`as_co_approver`) | `test_two_signatures_with_financial_control_post_once` |
| A duplicate delivery costs nothing | `orchestration/machine.py` (`_short_circuit_replay`) | `TestRepeatSignatureSpendsNothing` |
| One decision per invoice, across runs | `persistence/schema.sql` | `TestOneDecisionPerInvoiceAcrossRuns` |
| A store from a newer build is refused | `persistence/repository.py` | `TestSchemaVersioning`, `TestMigrationCrashWindows` |
| No credential-shaped string is committed | `scripts/secret_sweep.py` | run it; exits non-zero on any finding |

## 8. Observability

| Item | Value | Implemented in |
|---|---|---|
| Egress path | one; every event and log line passes through it | `observability/events.py` |
| Redaction | applied before anything is written | `observability/redact.py` |
| Mandatory event fields | timestamp, run identifier, correlation identifier, outcome, duration | `observability/events.py` |
| Duration clock | `time.perf_counter`, monotonic | `observability/events.py` |
| Event types | 19, append-only vocabulary | `domain/enums.py` |
| Log format | one JSON object per line | `AP_LOG_FORMAT` |
| Recorded for model calls | provider, model, schema, attempt, token counts | `observability/events.py` |
| Not recorded for model calls | prompts and completions, deliberately | `observability/events.py` |
| Storage | `events` table plus the process log | `persistence/schema.sql` |

## 9. Test and evaluation configuration

| Tier | Command | Count | Model | Network |
|---|---|---:|---|---|
| Unit | `pytest tests/unit` | 291 | none | none |
| Contract | `pytest tests/contract` | 161 | none | none |
| Evaluation | `pytest tests/eval` | 147 | deterministic adapter | none |
| Live model | `pytest -m live_model` | 1 | Claude | required |
| Fixture cases | `ap-agent eval` | 5 cases | deterministic adapter | none |
| Retrieval quality | included in `ap-agent eval` | 16 queries | none | none |

Evaluation runs use a fixed clock, 2026-09-11T09:30Z, and fixture dates are expressed
relative to it, so a case keeps its intent rather than its values as the calendar moves.

## 10. Environment

| Item | Value |
|---|---|
| Environment | local. No cloud resources are created. |
| Language and runtime | Python 3.12 or later; developed on 3.14 |
| Dependency lock | `uv.lock` |
| Container | `infra/Dockerfile`, `infra/compose.yaml` |
| Local state | `data/index/` and `data/runtime/`, both removable |
| Cost | nothing in the default configuration; the live tier is two model calls per case |
