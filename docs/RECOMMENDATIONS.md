# Recommendations

What I would do next, in priority order. Each item states the gap, the change, and why it
sits where it does. The ordering is by risk reduction per unit of effort, not by how
interesting the work is.

Items 1 to 4 are the ones I would not deploy without.

---

## 1. Enforce the second approver, do not merely record it

**Gap.** FIN-POL-003 §3 requires two approvals for a higher-risk transaction, one of them
from Financial Control. The engine computes that requirement, records it on the
recommendation and stores it on the approval request, and the approver sees it. Nothing
enforces it: a single sufficient approver completes the run.

**Change.** Model the approval as a set of required signatures rather than one decision. The
gate opens when every required signature is present, distinct, and each signatory is within
their own authority. The `approvals` table grows a `signatures` child table; the gate
function changes from "is this approval APPROVED" to "are the required signatures
collected".

**Why first.** It is the only place where the system computes a control correctly and then
does not apply it. Every other gap on this list is a missing capability; this one is a
control that looks enforced and is not, which is worse than an absent control because it
invites reliance.

## 2. Approver identity from an identity provider

**Gap.** The approver identifier and role arrive in the callback body and are taken at face
value. Authority is validated against the matrix and the register, but *who the caller is* is
not. Anyone who can reach the endpoint can claim to be a Department Director.

**Change.** Single sign-on with the role read from the directory, not the request. The
FIN-POL-003 §5 evidence fields populate from the identity provider. Segregation of duties
(FIN-POL-001 §4) moves from comparing two identifier strings to consulting the
organisational graph, which is what the policy actually means.

**Why second.** The approval gate is the system's central control and currently rests on an
unauthenticated assertion. It is second only because item 1 concerns a control that is
already computed, whereas this one needs an external dependency the take-home environment
does not have.

## 3. Authentication and rate limiting on the HTTP surface

**Gap.** The service has neither. It binds to localhost, which is a deployment convention
rather than a control.

**Change.** Authentication at the edge, authorisation per endpoint, and a rate limit on
`POST /runs` so a caller cannot exhaust the model budget or fill the store. The container
already drops capabilities and runs read-only apart from its state volume; this is the
missing half.

**Why third.** Necessary before any deployment, but it protects a system whose internal
controls are already sound, so it ranks below the two items that concern those controls.

## 4. Durable execution for the driver

**Gap.** The state machine and its SQLite checkpointing are hand-written. They are correct
for the failure modes tested here, and the exactly-once decision guarantee rests on schema
constraints rather than on the driver, which is the right place for it. But the driver is the
component most likely to be subtly wrong under partial failure at volume: a process that dies
between calling the posting adapter and persisting the receipt leaves a decision recorded and
a run that does not know it.

**Change.** Temporal or an equivalent. Phases become activities; the state machine becomes a
workflow; the engine supplies at-least-once activity execution with durable timers, and the
existing idempotency key makes at-least-once safe. This is the production substitute recorded
in [ADR-0002](adr/0002-orchestration-framework.md) and [ADR-0004](adr/0004-persistence-and-idempotency.md).

**Why fourth.** The window it closes is narrow and the current design already survives the
failure modes that have tests. It is on the "would not deploy without" list because the
window is narrow, not absent, and it involves money.

---

## 5. PostgreSQL in place of SQLite

The schema ports directly; `BEGIN IMMEDIATE` becomes `SELECT ... FOR UPDATE`. The repository
is the only module containing SQL, so the change is contained to one file.

What this actually buys is concurrent writers without the process-local lock the current
design needs, and a store that more than one service instance can share. Worth doing when
there is a second instance, and not before: SQLite in WAL mode is genuinely adequate at one
writer, and swapping it earlier would add an operational dependency for no property the
system does not already have.

## 6. Vector store with permission filtering and deletion propagation

Two policy requirements are currently modelled rather than met. FIN-POL-010 §3 requires
retrieval to enforce source permissions *before* returning chunks; the retriever filters on a
`classification` field, which is the right shape but has no identity to filter against.
FIN-POL-010 §5 requires deleting a source document to remove or expire its derived index
entries; the index is rebuilt wholesale from a corpus hash, so deletion works by accident
rather than by design.

A managed vector store with row-level access control and tombstone propagation addresses
both. Note that this is about *access control and lifecycle*, not about retrieval quality:
Hit@3 is already 1.00 on the golden set, and adding embeddings would not improve a measure
that is saturated.

## 7. Foreign-exchange rate service

The system holds a foreign-currency invoice against an AUD order rather than converting it,
which is what FIN-POL-009 §3 requires when the order does not permit conversion. It cannot
process the *legitimate* converted case, because FIN-POL-009 §2 requires a cited rate with
its source and date, and inventing one would produce an authority decision with no auditable
basis.

A rate service supplying rate identifier, source and date turns a hold into a decision. The
`Calculation` record already has fields for the inputs and the policy reference, so the
conversion would be auditable the moment the rates are.

## 8. Public-holiday calendar

Business-day arithmetic currently skips weekends only, because the corpus references public
holidays (FIN-POL-006 §2) without supplying a calendar. Exception review dates and
payment-run scheduling are therefore indicative, which the README states. A jurisdiction
calendar per legal entity makes both exact. Small, self-contained, and the kind of
approximation that quietly produces wrong due dates if left in place.

## 9. Retrieval improvements, once there is a reason

Three things I deliberately did not do, with the condition that would change my mind:

**Hybrid dense retrieval.** Implemented behind a flag and off by default. Hit@3 is 1.00 on
the golden set, so there is nothing to improve; adding a 100 MB model download to the install
step for a saturated measure is paying a cost for no benefit. Turn it on when the golden set
grows to include paraphrased queries and Hit@3 falls.

**Tuned BM25 parameters.** Left at the library defaults on purpose. Tuning k1 and b on 15
documents fits the golden set rather than the task, and the result would look like an
improvement while generalising worse.

**A cross-encoder re-ranker.** The standard next step, and premature here. It would add
latency and a second model to a ranking stage that already places every golden target in the
top three.

What I would do instead, when the corpus grows: expand the golden set first, and let the
measurement decide. The set is 16 queries against 15 documents; it is sized to the corpus,
and a larger corpus needs a larger set before any ranking change can be justified.

## 10. Strengthen injection detection beyond a heuristic

The current detector is anchored to clause boundaries and is honest about what that misses: a
well-formed attack in the passive voice, in another language, or encoded. It is deliberately
a *secondary* control, and the structural controls do not depend on it.

Two changes worth making, in this order. First, a second detector that looks at the *effect*
rather than the text: any evidence asserting a bank change, an approval, or an urgency that
the systems of record do not corroborate is a contradiction, and contradiction detection does
not care what language the assertion was written in. Second, an adversarial corpus larger
than one document, so the detector's false-negative rate is measured rather than asserted.

I would not invest in better pattern matching. Pattern matching is the part of this that
cannot be made reliable, which is exactly why the design routes around it.

## 11. OpenTelemetry export

The event emitter already carries a correlation identifier, durations and outcomes on every
event, and records provider, model and token counts on every model call. Exporting spans is a
change of sink rather than of structure: one exporter, and the generative-AI semantic
conventions map onto the existing `MODEL_CALL` payload almost field for field.

Listed here rather than higher because the data already exists and is queryable from the
`events` table. This buys convenience and cross-service correlation, not visibility the
system lacks.

## 12. A review queue for held and escalated cases

Three of the five fixture cases end in `HELD` or `ESCALATE_CONTROL_REVIEW`, and in production
most cases would. At present they end and sit in the database. FIN-POL-007 §4 sets service
levels, prioritises invoices due within two business days, and requires escalation to the
Accounts Payable Manager after ten business days.

None of that can be honoured without a queue that knows a case's age and owner. The exception
records already carry the owner and the review date, so the data model is ready; what is
missing is something that reads it and acts.

## 13. Cost budgets per run

Token counts are recorded per model call, so a per-run ceiling is a gate over data the system
already has rather than new instrumentation. Add it when the live provider is the default;
with the deterministic adapter the cost is zero and a budget would guard nothing.

---

## What I would not change

Worth stating, because a recommendations list that only adds things implies everything present
is provisional.

**Policy thresholds stay as constants in code.** Reading them from retrieved text would make
the authority matrix vulnerable to retrieval quality and to any document asserting a higher
limit. Retrieval supplies citations; the numbers come from `domain/rules/`. In production the
constants become policy *data* with versioning and an approval workflow of their own, but they
do not become model input.

**The model stays out of the decision.** Its two phases produce narrative and structured
readings of evidence. Widening that to let it weigh controls would trade the property that
makes the injection tests meaningful for a fluency the system does not need.

**The rule engine stays pure.** No I/O, no clock of its own, no hidden state. That is what
lets every threshold be tested at its boundary and every run be re-derived from its persisted
evidence.

**Read tools keep returning results rather than raising.** A run must be able to continue
with a recorded gap, because FIN-POL-001 §5 requires missing evidence to produce a hold, and
it cannot hold if the missing evidence crashed the run.

**The write tool keeps its own authorisation check.** It duplicates the orchestrator's gate on
purpose. A single enforcement point for the property that keeps money from moving is a single
point of failure.
