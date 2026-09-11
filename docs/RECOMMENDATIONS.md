# Recommendations

What I would do next, in priority order. Each item states the gap, the change, and why it
sits where it does. The ordering is by risk reduction per unit of effort, not by how
interesting the work is.

Items 1 to 3 are the ones I would not deploy without.

The first release's top recommendation was to enforce the second approver rather than merely
record it. That is now done, along with several other controls the corpus requires, so those
items have left this list; the section at the end says what was delivered and what it replaced.

---

## 1. Approver identity from an identity provider

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

## 2. Authentication and rate limiting on the HTTP surface

**Gap.** The service has neither. It binds to localhost, which is a deployment convention
rather than a control.

**Change.** Authentication at the edge, authorisation per endpoint, and a rate limit on
`POST /runs` so a caller cannot exhaust the model budget or fill the store. The container
already drops capabilities and runs read-only apart from its state volume; this is the
missing half.

**Why third.** Necessary before any deployment, but it protects a system whose internal
controls are already sound, so it ranks below the two items that concern those controls.

## 3. Durable execution for the driver

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

## 4. PostgreSQL in place of SQLite

The schema ports directly; `BEGIN IMMEDIATE` becomes `SELECT ... FOR UPDATE`. The repository
is the only module containing SQL, so the change is contained to one file.

What this actually buys is concurrent writers without the process-local lock the current
design needs, and a store that more than one service instance can share. Worth doing when
there is a second instance, and not before: SQLite in WAL mode is genuinely adequate at one
writer, and swapping it earlier would add an operational dependency for no property the
system does not already have.

## 5. Vector store with permission filtering and deletion propagation

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

## 6. Foreign-exchange rate service

The system holds a foreign-currency invoice against an AUD order rather than converting it,
which is what FIN-POL-009 §3 requires when the order does not permit conversion. It cannot
process the *legitimate* converted case, because FIN-POL-009 §2 requires a cited rate with
its source and date, and inventing one would produce an authority decision with no auditable
basis.

A rate service supplying rate identifier, source and date turns a hold into a decision. The
`Calculation` record already has fields for the inputs and the policy reference, so the
conversion would be auditable the moment the rates are.

## 7. Public-holiday calendar

Business-day arithmetic currently skips weekends only, because the corpus references public
holidays (FIN-POL-006 §2) without supplying a calendar. Exception review dates and
payment-run scheduling are therefore indicative, which the README states. A jurisdiction
calendar per legal entity makes both exact. Small, self-contained, and the kind of
approximation that quietly produces wrong due dates if left in place.

## 8. Retrieval improvements, once there is a reason

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

## 9. Strengthen injection detection beyond a heuristic

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

## 10. OpenTelemetry export

The event emitter already carries a correlation identifier, durations and outcomes on every
event, and records provider, model and token counts on every model call. Exporting spans is a
change of sink rather than of structure: one exporter, and the generative-AI semantic
conventions map onto the existing `MODEL_CALL` payload almost field for field.

Listed here rather than higher because the data already exists and is queryable from the
`events` table. This buys convenience and cross-service correlation, not visibility the
system lacks.

## 11. A review queue for held and escalated cases

Two of the five fixture cases end in `HELD` or `ESCALATE_CONTROL_REVIEW`, and in production
most cases would. At present they end and sit in the database. FIN-POL-007 §4 sets service
levels, prioritises invoices due within two business days, and requires escalation to the
Accounts Payable Manager after ten business days.

None of that can be honoured without a queue that knows a case's age and owner. The exception
records already carry the owner and the review date, so the data model is ready; what is
missing is something that reads it and acts.

## 12. Finish decomposing the orchestrator

**Gap.** `orchestration/machine.py` is 1,826 lines. ADR-0002's case for going framework-free
rests on a reviewer being able to read the orchestrator end to end, and an audit was right that
the file had outgrown the argument. Three modules came out of it in the last pass
(`narrative_screen.py`, `summaries.py`, `approvals.py`), which took it from 2,154, but the seven
phase handlers and `_resolve` are still there, and `_resolve` alone is 221 lines.

**Change.** Move each phase into `orchestration/phases/<name>.py` with the driver unchanged.
The obstacle is not the phase bodies, it is what they reach for: the repository, the clock, the
model client, the budget, the retriever, the tool runner and four orchestrator helpers. Handing
each module the orchestrator would be a rename rather than a decomposition, so the work is to
define a narrow `PhaseContext` protocol carrying exactly those collaborators, which also makes
each phase testable without constructing a run.

`_resolve` is the harder half and should stay last. It calls the driver, the emitter and the
finaliser, so it is genuinely orchestration rather than a phase, and extracting it would move
control flow away from the module named for it.

**Why here.** It is a readability defect, not a correctness one, and the safety properties are
tested where they are enforced rather than where they are written. But it sits above the
retrieval items because a monolithic hotspot is where subtle regressions concentrate, and this
codebase was written with heavy AI assistance, which makes that concentration more likely rather
than less.

## 13. Contextual chunk annotation, if paraphrase recall has to improve

**Gap.** Paraphrase recall is 0.38 and hybrid retrieval buys only 0.50 at the cost of direct
precision, so neither option is currently worth switching on. The technique that would help
without a model at query time is prepending a short situating sentence to each chunk before
indexing, so a section inherits its document's vocabulary.

**Change.** Generate the situating line deterministically from the front matter already parsed
at ingestion (document title, section heading, tags) rather than with a model, keeping ingestion
free of a provider. Measure on the paraphrase set; keep only if it moves.

**Why here.** It is the cheapest remaining lever on the one retrieval number that is weak, and
unlike the hybrid flag it costs nothing at query time. It is below the reliability items because
0.38 is a measured cost of a deliberate choice, not a defect.

## 14. Raise the tool budget derivation to account for a second signature

**Gap.** The default of sixteen tool attempts is derived from the worst case of a single
approval. A two-signature approval resolves twice, and each resolution naming a delegation
reads the authority register, so a higher-risk run with two delegated approvers needs
seventeen. The budget is a setting, so the effect is a refusal at the second signature rather
than anything unsafe, but the derivation no longer matches the flow it was derived from.

**Change.** Derive the ceiling from the phase plan *plus* the maximum signature count, and
assert the derivation in a test so the two cannot drift again.

**Why here.** It is a correctness issue in a documented number rather than in behaviour, and
it is visible only at a configuration boundary a deployment would raise anyway.

## 15. A configurable tax profile

**Gap.** `domain/rules/tax.py` carries one rate, one tolerance and an implicit assumption that
a domestic invoice is an Australian one. The corpus states a jurisdiction and no rate, so a
constant was the honest choice for one jurisdiction, and it is the wrong shape for two.

**Change.** A tax profile in settings: rate, rounding tolerance, the currencies it applies to,
and the exempt categories a supply may claim. The rule reads the profile; nothing else changes.

**Why here.** Nothing derived from the rate blocks a case today, so this buys correctness of
reporting rather than of decisions. It becomes urgent the moment a second jurisdiction appears.

## 16. Cost budgets per run

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

---

## Delivered since the first release

These were on this list, or were found by review of it, and are now implemented. They are
recorded here because a recommendations list that silently drops items is not a record of
anything.

| Was | Now | Where |
|---|---|---|
| The second approver was computed and not enforced | Approvals accumulate signatures; the gate opens on count, distinctness and a Financial Control signature | [adr/0007](adr/0007-two-signature-approvals.md) |
| One decision per *run* | One decision per run and one per *invoice*, across runs | `persistence/schema.sql` |
| The schema was applied with `IF NOT EXISTS` and never versioned | `user_version` with ordered forward migrations; a newer store is refused | [adr/0006](adr/0006-schema-versioning-and-migration.md) |
| `REJECT_INVALID` was unreachable | `domain/rules/validity.py` | FIN-POL-001 §2, §3 |
| `TAX_QUERY` was unreachable | `domain/rules/tax.py` | FIN-POL-002 §2 |
| Payment terms were carried and never read | `domain/rules/payment_terms.py`, with a proposed standard run | FIN-POL-006 §1 to §3 |
| Payment instructions were never compared with the vendor master | `domain/rules/payment_instructions.py` | FIN-POL-001 §5 |
| Delegation scope was stored and never compared | `domain/rules/authority.py` | FIN-POL-003 §4 |
| Repeated non-PO purchasing was not detected | `domain/rules/non_po.py` | FIN-POL-012 §4 |
| A credit note was processed as an invoice | Recognised and held for Financial Control | FIN-POL-008 §1 |
| Hybrid retrieval was documented and absent | Implemented, measured in both modes, and refused rather than silently ignored when misconfigured | [adr/0003](adr/0003-retrieval-strategy.md) |
| The golden set could not fail for the reason lexical retrieval fails | Paraphrase and unanswerable queries added; both reported apart from the gated numbers | `tests/eval/retrieval_golden.json` |
| The narrative screen was a list of eight phrases | Claims crossed with recorded state, and every figure crossed with the computed set | `orchestration/narrative_screen.py` |
| Generation quality had no measure at all | `narrative_grounded` per case, reported beside the retrieval metrics | `evaluation/runner.py` |
| A provider failure reported an internal error with an invented retry history | The provider's own message, and a permanent status named as permanent | `llm/anthropic_client.py` |

The pattern across them is worth naming, because it is the first thing I would look for in a
system like this. Four of the ten were not missing code at all. They were fields or
requirements that the system *stored and never read*: the delegation's scope, the invoice's
payment terms, the approval's second-signature requirement, and the `invalid_reasons` list
that made `REJECT_INVALID` reachable in the type system and unreachable in practice. Each
appeared in the run output, so each looked implemented, and each was inert.

That failure mode is harder to find than an absent control and worse to ship, because a
control that is displayed but not applied invites exactly the reliance an absent one would not.
It is also why the tests added with these fixes assert on behaviour at the gate rather than on
the presence of a field.
