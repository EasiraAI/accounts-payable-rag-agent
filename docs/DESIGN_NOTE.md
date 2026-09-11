# Design note

Accounts-payable processing and retrieval-augmented generation workflow agent.
Decision records with the rejected alternatives are in [adr/](adr/).

## 1. Context and goals

The system takes an invoice-processing request and produces a cited recommendation an
approver can act on. Four properties drove every structural choice.

**Money must not be able to move without a human decision.** This is not a feature to be
added at the end; it is what the architecture is shaped around.

**Arithmetic must be reproducible.** FIN-POL-002 §5 makes model-generated arithmetic
non-authoritative and requires inputs, formula, result and rounding to be stored. A finance
reviewer must be able to re-perform any calculation by hand.

**Evidence is untrusted.** Retrieved documents and case text may be incomplete,
contradictory, stale, or written to manipulate the system. The corpus contains all four:
a superseded authority matrix, an adversarial supplier notice, and a topically irrelevant
travel policy with plausible monetary limits.

**A run must be explainable after the fact.** Not "the model concluded", but which documents
were retrieved, which controls were evaluated, what each computed, and what remains unknown.

Non-goals: a user interface, real payment rails, real enterprise-resource-planning
integration, multi-tenant authentication.

## 2. Orchestration

An explicit state machine in plain Python, no agent framework. Phases:

```
INTAKE -> RETRIEVE_POLICY -> GATHER_EVIDENCE -> RECONCILE -> ASSESS_RISK -> RECOMMEND
                                                                              |
                                    +-----------------------------------------+
                                    |                                         |
                         requires approval                        no approval needed
                                    |                                         |
                          AWAITING_APPROVAL --approve--> EXECUTE_DECISION      |
                                    |                          |              |
                                 reject                         v              v
                                    v                     COMPLETED          HELD
                                  HELD
```

### What the framework would have provided, and what the code enforces

LangGraph was the closest off-the-shelf match to the brief's "explicit state graph" wording,
and it would have supplied the graph runtime, checkpointing, interrupt-and-resume, streaming
and tracing hooks. Set against that, everything the reviewer will actually probe remains the
application's responsibility under any framework:

| Property | Framework | This code |
|---|---|---|
| Graph execution and checkpointing | provided | ~150 lines with SQLite |
| Interrupt and resume | provided | phase field plus a reload |
| Deterministic reconciliation | not provided | `domain/rules/` |
| Approval semantics | not provided, a framework pauses but does not know what an approval *is* | `orchestration/gates.py` |
| Exactly-once decision | not provided; an interrupt resumes by re-executing the node | schema constraints |
| Prompt fencing | not provided | `llm/prompts.py` |
| Output validation | partially | one structured method, one repair |
| Safe logging | not provided | one redaction path |

Given that split, the framework would have added an audit surface without removing work. A
300-line orchestrator a finance-controls reviewer can read end to end is a better
demonstration than a graph definition plus a dependency they must take on trust. The
trade-off is real: at roughly ten or more phases, or with streaming or parallel branches
required, the hand-written driver stops paying for itself. That threshold is recorded in
[ADR-0002](adr/0002-orchestration-framework.md).

### Bounding

Two finite counters. Steps count phase executions; tool calls count *attempts*, so a retry
spends budget. A retry that cost nothing would make the budget describe an intention rather
than a bound: a flapping dependency could otherwise consume unbounded wall-clock time inside
a run whose step count looked healthy.

Both defaults are derived from the phase plan rather than chosen as round numbers, so
exhausting either means something genuinely unexpected happened. The worst case is sixteen
tool attempts: four policy retrievals, one conditional evidence retrieval, three vendor
attempts, three purchase-order attempts, three history attempts, one delegation lookup, one
decision.

### Tools are called by need, not by loop

With a free-running agent loop, "call tools only when needed" is a property one hopes the
model exhibits. Here it is a property of the plan, with a stated precondition per tool: the
vendor and history lookups are mandatory under FIN-POL-001 §5 and FIN-POL-005 §1; the
purchase-order lookup happens only when the case names an order; the evidence retrieval
happens only when the case carries free text or the vendor shows a recent bank change; the
delegation lookup happens only when an approval callback names one. A reviewer can therefore
tell whether any call was justified, and no retrieved document can cause one.

### Where the model sits

Two phases call the model and neither decides anything. `ASSESS_RISK` reads retrieved text
into structured statements; `RECOMMEND` writes the narrative an approver reads. The outcome
is computed by the rule engine from typed facts *before* the model is asked for prose.

The model may return a `suggested_outcome`, applied only when it is strictly more
conservative than the computed one. The asymmetry is the point: the failure mode worth
guarding against is an over-permissive model, and an injected instruction can only ever argue
for permission. A suggestion that would loosen the outcome is recorded as a finding and
discarded, which is asserted by a test that configures the adapter to behave as though it had
obeyed the corpus's injected instruction.

## 3. RAG design

**Chunking: one chunk per second-level Markdown heading.** Not a generic default. Every
policy cross-reference in this corpus takes the form "FIN-POL-003 §4", and every heading is
numbered to match, so a retrieved chunk maps exactly onto a reference a reviewer would write
by hand and can be verified by opening the document and reading one section. Fixed-size
windows would split a tolerance table from the sentence qualifying it and produce citations
nobody can check; whole documents would put the entire authority matrix into context for a
freight-tolerance question; sentence chunks would strip the heading that gives a threshold
its scope, and "the limit is AUD 100 or 2%" is dangerous without the section saying it
applies to services.

**Index: BM25 over section chunks, JSON-persisted.** Queries here quote the policy text
rather than paraphrasing it: "three-way match tolerance", "delegated authority", "goods
receipt" are not synonyms of the corpus, they are extracts from it. BM25 scores exactly that
overlap and does so deterministically, so the grounding tests run in continuous integration
with no model, no network and no flaky ranking. Dense retrieval earns its place on
paraphrased queries, which is why a hybrid mode exists behind a flag; it is not the default
because adding a 100 MB download to improve recall on paraphrases the corpus does not contain
would be paying a cost for an undemonstrated benefit. The index is JSON rather than a pickle,
so the retrieval path never deserialises executable content from disk.

### Handling the corpus traps

Neither chunking nor ranking addresses them. Three mechanisms do, in decreasing strength:

1. **Type filtering.** A policy lookup requests `doc_types=("policy",)` and cannot return
   anything else. No score can defeat a filter. This keeps the travel policy's meal
   allowances out of a tolerance question and the adversarial notice out of policy synthesis.
2. **Status demotion.** A superseded document's score is multiplied by 0.3. Demotion rather
   than exclusion: the document is legitimate history, an auditor may need it, and
   FIN-POL-003-OLD's own text asks a retrieval system to "rank current policy above this
   document and expose its superseded status". No ranking function could do this, because the
   distinguishing fact is a date in the front matter.
3. **Labelling.** Every chunk carries its status, and every citation renders a non-current
   status visibly. This is the weakest mechanism and is never relied on alone: asking a model
   to disregard a document it can read is a request, not a control.

### Two measured corrections

Both were found by the golden set, not by reasoning.

*Compound tokens.* Emitting a hyphenated word both whole and split tripled the weight of
common phrases: "purchase-order" contributed three tokens where plain "purchase order"
contributed two. A records-retention section that merely lists document types outranked the
emergency-purchase policy a question was actually about. Ordinary compounds are now split
without the whole form; identifiers, recognised by containing a digit, keep it so an exact
reference still scores highest.

*Query-term duplication.* BM25 as published scores over the *set* of query terms, with
repetition handled by a separate query-term-frequency factor that `rank_bm25` does not
implement. Passing duplicates therefore counted a term twice. Golden query Q09 repeats
"purchase", and deduplicating moved Hit@3 from 0.94 to 1.00 and MRR from 0.919 to 0.969 with
no query regressing.

### Measurement

Sixteen golden queries, each phrased as an analyst would ask rather than copied from the
target section, so the measure is of retrieval and not of string equality.

| Metric | Value | Gate |
|---|---|---|
| Hit@1 | 0.94 | reported, not gated |
| Hit@3 | 1.00 | ≥ 0.90 |
| Mean reciprocal rank | 0.969 | ≥ 0.88 |
| Distractor leaks | 0 | must be 0 |
| Stale-policy leaks | 0 | must be 0 |

Hit@3 and MRR are the operative measures because the agent consumes a top-k window rather
than a single best result. Gating on Hit@1 would optimise for a consumption pattern the
system does not have and push towards over-fitting the golden set. The two safety checks are
absolute rather than statistical: an average that tolerates one leak is not a control.

### Limitations

Lexical default misses pure-synonym queries. BM25 parameters are untuned, deliberately: 15
documents cannot support tuning without fitting the golden set. BM25 cannot represent
negation, so "no purchase order" matches documents about purchase orders. Permission
filtering (FIN-POL-010 §3) is a metadata filter on `classification`, not enforcement against
an identity provider. Demotion weights are policy choices, not learned values. The index is
rebuilt wholesale; there is no incremental update or deletion propagation, which
FIN-POL-010 §5 would require in production.

## 4. Trust boundaries

Three levels. Nothing crosses upward.

| Level | Contents | May |
|---|---|---|
| Policy (trusted) | system prompt, orchestration code, rule engine, tool permission table | decide, act |
| Evidence (untrusted) | retrieved chunks, vendor, order and history records | be quoted, cited, compared |
| Case input (untrusted) | request notes, attachments, invoice text | be quoted, cited, compared |

Enforced structurally, not by asking the model nicely:

1. **Capability separation.** The model never calls the write tool. It produces a proposal;
   the orchestrator checks the stored approval and calls the tool; the tool re-checks
   independently. Two gates, because a single enforcement point for the property that keeps
   money from moving is a single point of failure.
2. **No override arguments.** No tool schema contains `force`, `skip_checks`, `verified`,
   `override`, or a caller-supplied idempotency key. A document can ask for anything; there
   is no field through which the request could be expressed. The safety property comes from
   the absence of a field, which a test asserts.
3. **Deterministic outcome.** Computed from typed facts by pure functions.
4. **Nonce-delimited fencing.** Untrusted text enters a prompt only through one function,
   inside a block whose boundary token carries a per-run random value. A fixed delimiter can
   be closed by the injected document itself; an attacker who cannot see the nonce cannot
   forge the boundary. Any occurrence of the boundary inside untrusted content is stripped as
   well, so forgery is impossible rather than merely improbable.
5. **Injection as a signal.** Imperative clause-initial language in case content increments
   the FIN-POL-005 §3 indicator count. The detector is anchored to clause boundaries because
   FIN-POL-005 *itself* contains "a request to bypass normal approval" and "tells the agent
   to disable checks"; a naive matcher would fire on the policy and be switched off within a
   day. The distinguishing feature is grammatical mood: an attack issues commands, policy
   describes them.
6. **Grounded citations.** The model returns chunk identifiers, never citation objects. Each
   is resolved against what this run actually retrieved and anything unrecognised is
   discarded and recorded. A model that invents "FIN-POL-002 §9" produces an identifier
   matching nothing, so the fabricated citation cannot reach the recommendation.
7. **One redaction path.** Every event and log line passes through one function. Bank numbers
   to the last four digits, tax identifiers and credentials removed. Tests assert on the
   stored payloads, and a further test confirms that the *injected instruction itself* is
   preserved: redaction removes secrets, not evidence, and a reviewer must be able to see
   what was attempted.

### An indicator-attribution defect worth recording

An early version fed every retrieved untrusted document into the fraud-indicator count. The
duplicate-invoice case then escalated instead of being rejected, because the evidence search
had surfaced the adversarial notice concerning an entirely different supplier and counted its
urgency and injection language against the case in hand.

Indicators describe a *transaction*. A document merely present in the corpus says nothing
about this one, and counting it would let any case be escalated by planting a document.
Indicators now come only from case-attached text and the vendor master; retrieved untrusted
documents are screened and logged as a corpus-hygiene observation. The evaluation report
reports the two separately so they cannot be confused again.

## 5. Model and tool contracts

One adapter protocol with a single method returning a validated pydantic model. There is no
method returning free text, so no caller can consume unvalidated output. On a validation
failure the adapter retries once with the error text; a second failure raises and the run
fails explicitly. One repair rather than a loop, because a retry loop around a model call is
the unbounded-loop failure mode the brief asks to avoid. There is deliberately no free-text
fallback: a regular expression over prose that failed to validate is how silently wrong
evidence enters a financial control.

Six tools, each declaring purpose, permission, timeout and a finite retry count.

| Tool | Permission | Timeout | Attempts |
|---|---|---|---|
| `retrieve_finance_documents` | READ | 5.0s | 2 |
| `get_vendor_record` | READ | 2.0s | 3 |
| `get_purchase_order` | READ | 2.0s | 3 |
| `check_invoice_history` | READ | 2.0s | 3 |
| `get_authority_delegation` | READ | 2.0s | 3 |
| `submit_finance_decision` | WRITE | 5.0s | 1 |

The write tool is not retried. A write is made safe by its idempotency key, not by a retry
loop, so that a partially-applied attempt cannot be compounded by a second one.

Failures are classified, not string-matched. Transient failures and timeouts are retried;
permanent failures are not, because an unknown vendor identifier will still be unknown on the
third attempt and spending budget on it starves a later control. A response that fails its
own output schema is treated as permanent: a tool returning the wrong shape is broken, not
flaky.

Two output shapes, as the brief requires, both present on every run. `Recommendation` carries
cited evidence, calculations, assumptions, confidence, exceptions and the next action; it is
what an approver reads. `FinalResult` carries sourced facts, calculations, inferences,
unknowns, policy findings and actions taken; it is what an auditor reads. Keeping them
separate is not bookkeeping: the approver needs a decision and its basis, the auditor needs
the epistemic status of every statement, and one flat object would force each audience
through the other's fields.

## 6. Persistence and resume

SQLite in WAL mode, four tables. Two constraints carry safety weight rather than hygiene:

```sql
decisions.idempotency_key  PRIMARY KEY   -- a duplicate callback collides
decisions.run_id           UNIQUE        -- a run records at most one decision, ever
```

The obvious implementation of "record this once" is to look for an existing row and write if
absent. That is wrong under concurrency and under crashes, because between the read and the
write another caller can write, or this process can die having already called the posting
adapter. Either way the invariant is lost. So uniqueness is declared in the schema and the
collision *is* the detection mechanism. `BEGIN IMMEDIATE` takes the write lock up front, so
concurrent callers serialise at the database rather than racing in application code.

The key is derived in code from the run, the approval and the outcome, never accepted from a
caller: a caller-supplied key could be varied to defeat the guarantee, which is the opposite
of what an idempotency key is for. A second delivery returns the *stored* response, so
responses are byte-identical across deliveries. The same key with different arguments is a
conflict rather than a replay, because returning the stored response would answer a different
question from the one asked.

A test proves this with six threads on six connections, which is what a second process looks
like: one effective call, six identical responses, one row.

Runs use optimistic versioning. Rather than locking a run for a phase's duration, which would
strand it if the holder died, each write asserts the version it read. A stale write is
rejected and the caller reloads; the failure mode is a retry, not a stuck run.

Resume is not a separate code path. A new process loads the stored state and re-enters the
driver at the recorded phase. Every phase in the linear plan performs reads and pure
computation, and the state accumulators suppress duplicates, so re-running one converges
rather than double-counting. `EXECUTE_DECISION` is the single phase with an external effect,
made safe by the idempotency key rather than by being repeatable.

### A concurrency defect worth recording

One instance holds one connection shared across threads. SQLite permits that, but a
connection has a single transaction context, so two threads issuing `BEGIN IMMEDIATE` collide
with "cannot start a transaction within a transaction". A concurrent-append test hit it on
eight of twelve threads. Writes through one instance are now serialised by a process-local
lock. The distinction matters: the lock stops threads interleaving, the constraints stop
processes double-posting, and only the second survives a crash. The exactly-once guarantee
rests on the constraints, which is why the test that proves it uses one connection per
thread.

## 7. Failure handling

| Failure | Handling | Observable |
|---|---|---|
| Tool timeout | Thread-based ceiling, bounded retry, then an explicit unknown | `TOOL_CALL` with `outcome=TIMEOUT`, attempt number, duration |
| Transient tool error | Retried with exponential backoff inside the budget | `TOOL_CALL` with `outcome=TRANSIENT_ERROR` |
| Permanent tool error | Not retried | `TOOL_CALL` with `outcome=PERMANENT_ERROR` |
| Tool response fails its schema | Treated as permanent | same |
| Malformed model output | One repair with the validation error, then `FAILED(MODEL_OUTPUT_INVALID)` | `MODEL_CALL` with `outcome=REPAIRED` or `INVALID_OUTPUT` |
| Provider unreachable | Run fails without deciding | `MODEL_CALL` with `outcome=UNAVAILABLE` |
| Duplicate approval | Detected before any validation or tool call; stored response returned | `APPROVAL_REPLAYED` |
| Budget exhausted | `FAILED(BUDGET_EXHAUSTED)` with the partial result retained | `BUDGET_EXCEEDED` |
| Restart mid-run | State reloaded, phase re-entered | `RUN_RESUMED` |
| Insufficient approver authority | Refused before the decision is accepted | `APPROVAL_RESOLVED` with `outcome=DENIED` |

Two principles run through the table. Missing evidence produces a *hold*, not a guess
(FIN-POL-001 §5), which is why read tools return a result object rather than raising: a run
must be able to continue with a recorded gap, and it cannot hold if the missing evidence
crashed it. And a failed run keeps everything it established, because the partial result is
what lets a human see how far it got and resume from the failed control, as FIN-POL-007 §5
requires.

Note the honest limitation in the timeout: a timed-out call is *abandoned*, not cancelled, so
the worker thread may still be running. Acceptable because every tool with a timeout is a
read. It would not be acceptable for the write tool, which is precisely why that tool is
idempotent.

## 8. Evaluation

Three tiers, separated by what they depend on rather than by speed.

| Tier | Count | Depends on | Protects |
|---|---|---|---|
| Unit | 183 | nothing | Rule thresholds at their boundaries, redaction, property-based invariants |
| Contract | 137 | nothing | Typed schemas, tool reliability, persistence and idempotency, HTTP surface |
| Evaluation | 104 | deterministic adapter | Retrieval grounding, the five cases, safety properties |
| Live model | 1 | external access | The same cases through a real model |

Fixture assertions live in the fixture files, so the expected control behaviour is declared
once and checked identically by the test suite, the command line and the HTTP endpoint.

Property-based tests cover the invariants example tests can only sample: that a variance
divided across any number of lines is still caught, that the required approver role is
monotone in amount, that splitting an amount never lowers the aggregate requirement, and that
decimal addition and subtraction round-trip exactly where float arithmetic does not.

The safety tier deserves particular note. Its most important test configures the model adapter
to behave as though it had obeyed the corpus's injected instruction and to ask for the
invoice to be approved, then asserts the run escalates anyway. That is the difference between
a system that asks a model to resist injection and one that does not depend on the answer.

## 9. What would change for production

**Durable execution.** The hand-written driver would be replaced by Temporal or an equivalent.
The hand-rolled checkpointer is adequate here and is the part most likely to be subtly wrong
under partial failure at volume.

**PostgreSQL.** Same schema; `BEGIN IMMEDIATE` becomes `SELECT ... FOR UPDATE`. The
repository is the only module containing SQL, so the change is contained.

**Real integrations behind the same contracts.** The four simulated backends become adapters
to the enterprise-resource-planning and vendor-master systems. The tool contracts do not
change, which is the point of having declared them.

**Vector store with permission filtering.** Retrieval enforces access before returning
chunks, per FIN-POL-010 §3, and deletion propagates to the index and its caches, per §5.

**Identity.** Approver identity from single sign-on rather than asserted in a callback body,
with the FIN-POL-003 §5 evidence fields populated from the identity provider. The second
approver is enforced rather than merely recorded.

**Secrets management.** Keys from a managed secret store rather than the environment, with
rotation.

**OpenTelemetry.** The event emitter already carries a correlation identifier, durations and
outcomes; exporting spans is a change of sink, not of structure.

**A review queue.** Held and escalated cases need somewhere to go, with service levels per
FIN-POL-007 §4 and the ten-business-day escalation in the same section.

**Authentication and rate limiting.** The service currently has neither.

## 10. Enterprise scale

**Volume and concurrency.** One driver per run, advanced by whichever worker holds it, with
optimistic versioning already in place. Workers pull from a queue; runs are independent, so
this scales horizontally. The exactly-once decision guarantee is already enforced at the
store and does not weaken with more workers.

**Multiple vendors and currencies.** Tolerance thresholds become policy data keyed by legal
entity and currency rather than module constants, resolved through the same rule functions.
A rate service supplies cited rates so FIN-POL-009 §2 can be satisfied rather than held
against.

**Corpus partitioning.** Documents are partitioned by legal entity and jurisdiction, and
retrieval filters on the requester's entitlements before ranking. The metadata filter already
in the retriever is the shape this takes; it needs an identity to filter against.

**Policy versioning.** Each run pins the policy versions it used, which the citations already
carry. A later policy change must not rewrite the basis of a historical decision, and an
auditor reopening a case must see the rules as they stood.

**Compliance.** Seven-year retention per FIN-POL-010 §1 in an append-only store with legal
hold suspending deletion. The event log is already append-only and redacted at a single
egress point, which is the property an auditor will test.

**Cost control.** Token counts are already recorded per model call, so a per-run budget with
a hard stop is a gate over data the system already has rather than new instrumentation.

**Segregation of duties at scale.** The engine already refuses self-approval and flags a
requester who created the vendor. At scale this needs the organisational graph rather than
two identifier comparisons.

## 11. Recommendations

In [RECOMMENDATIONS.md](RECOMMENDATIONS.md), in priority order with the reasoning for each.
