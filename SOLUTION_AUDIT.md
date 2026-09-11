# Independent Solution Audit: Agentic AP Invoice-Processing Take-Home

| | |
|---|---|
| Subject | Submission to the "Financial Processing and RAG Workflow Agent" take-home (Agentic AI Engineer) |
| Repository | `finance_rag_corpus/finance_rag_corpus` (Python 3.12+, ap-agent 1.0.0) |
| Auditor | Independent AI solution architecture review (Claude), commissioned by Learning Online Group |
| Date | 11 September 2026 |
| Mode | Audit only. No file in the submission was modified. |

---

## 1. Verdict up front

**Overall: 8.8 / 10 weighted. This is a strong senior-level submission, in several areas at or beyond current market best practice for regulated agentic workflows.** The architecture is the correct 2026 pattern for a financial control system: a deterministic rule engine decides, an LLM only narrates, consequential actions sit behind a schema-enforced, human-gated, exactly-once write path. The contrarian technology choices (no agent framework, lexical retrieval by default) are not shortcuts; they are argued in decision records with evidence and with named conditions for reversing them, which is precisely what the brief asked for.

The material weaknesses are evidentiary rather than conceptual: there is no committed proof the live-model path was ever exercised, generation quality has no metric at all, the retrieval evaluation is small and self-referential, and the orchestrator has grown into a 2,089-line module that contradicts its own ADR's readability argument. None of these undermines the control architecture; all of them are what a hiring panel should probe.

Scored against the brief's own assessment areas:

| Assessment area (brief §9) | Weight | Score | One-line justification |
|---|---:|---:|---|
| Agent and RAG architecture | 25% | 9.5 | Deterministic core with a narrating model matches the strongest published patterns (CaMeL-adjacent capability separation); explicit phases, preconditions, budgets |
| Reliability | 20% | 9.0 | Exactly-once enforced by four schema constraints, not control flow; resume, optimistic versioning, classified failures; hand-rolled driver and single-process limits are documented |
| Safety | 20% | 9.0 | Structural injection defence plus nonce spotlighting, narrative screening, conservative-only model influence; residual heuristics are English-only and acknowledged |
| RAG and evaluation | 15% | 7.5 | Retrieval measured properly for the corpus size with absolute leak gates; generation side has no quality measure and no live-model evidence |
| Engineering quality | 20% | 8.5 | Strict typing, three-tier deterministic tests, exceptional documentation; monolithic orchestrator module, no CI pipeline, one stale cross-reference, missing time declaration |

**Hire signal: strong senior, with staff-level control-engineering instincts, conditional on the live defence.** The submission declares substantial AI assistance (which the brief permits and the declaration handles unusually well), and the git history shows roughly 24,000 lines of source, tests and documentation produced in about five hours of wall clock. At that velocity the artefact cannot by itself prove the engineer holds the design in their head. The brief anticipates exactly this: §11 requires the author to explain, modify and defend every decision live. Section 8 of this audit supplies the probes.

---

## 2. What was asked and what was delivered

**The brief** (8-hour timebox): an accounts-payable agent that accepts an invoice case, retrieves policy through a RAG pipeline, gathers evidence through five specified tools, reconciles deterministically, produces a cited recommendation, stops for human approval before any consequential outcome, persists enough state to resume safely, and treats all retrieved and case content as untrusted. Five fixture cases (clean match, duplicate, poisoned document, missing evidence, duplicate approval), typed contracts, bounded execution, safe logging, and a design note with ADR-quality reasoning.

**The delivery**, verified file by file:

- About 15,000 lines of source across 50 Python modules in a clean layered layout: `domain` (typed contracts plus 12 pure Decimal rule modules), `rag`, `tools`, `llm`, `orchestration`, `persistence`, `observability`, `evaluation`, `api`, `cli`, one composition root.
- ~8,900 lines of tests in three deterministic tiers (unit 291, contract 161, evaluation 152 as documented; 570 test functions are present in the tree plus parametrised expansions, consistent with the claimed 604) and one live-model test excluded by default.
- Documentation beyond what most production teams keep: a 32KB design note, seven ADRs each recording rejected options, a component manifest, a prioritised 14-item recommendations file, a references file citing the relevant 2023-2025 security and RAG literature, five run transcripts, two walkthroughs, an evaluation report, and an AI-usage declaration.
- Working artefacts: BM25 index over 58 section chunks from the 15-document corpus, SQLite store with migrations, FastAPI surface matching the brief's four operations, CLI equivalents, two-stage non-root Dockerfile with compose file, setup scripts, a secrets sweep script, and a repository-scoped agentic-coding configuration (`.claude/` with review agents, hooks that block credential-shaped writes, and skills encoding the policy thresholds).
- All five fixture cases pass in the committed evaluation report, including the poisoned-document case escalating with the injection recorded as a fraud indicator and zero decisions recorded.

**Requirements coverage.** Every functional requirement in brief §4 is implemented and, in almost every case, tested by name: bounded loop (step and tool budgets, retries spend budget), validated model output (one structured method, one repair, then explicit failure), deterministic arithmetic (Decimal throughout, property-based tests), event logging with correlation IDs and redaction, the five failure modes (timeout, transient failure, malformed output, duplicate approval, restart/resume), untrusted-content handling, deny-by-default approval-gated idempotent write tool, and a typed final result separating facts, calculations, inferences, unknowns, findings and actions. Two compliance misses are noted in section 5: the approximate time spent is not declared anywhere (§11 asks for it), and the evidence for the live-LLM path is limited to code.

**Timeline (from git history).** Thirteen conventional commits between 11:22 and 15:15 AEST on 11 September 2026, roughly five hours after receiving the materials: scaffold and domain first, rules, RAG, persistence and state machine, API/CLI/tests, docs and container, then a distinct hardening phase (second-approver enforcement and seven control gaps closed, corrections from three further reviews, documentation reconciliation). Comfortably inside the 8-hour timebox, and the hardening commits corroborate the declaration's claim that adversarial self-review found and fixed real defects after the first "complete" version.

---

## 3. Method: what this audit did

Every document in the repository was read (README, design note, all seven ADRs, manifest, recommendations, references, AI declaration, plans and specs, sample transcripts and walkthroughs). The core source was read in full or in structured depth: RAG ingestion/index/retriever, prompt construction, both model adapters, tool runner and contracts, orchestration driver and key phases (narrative screening, approval resolution, decision execution), gates, persistence schema and the decision-recording and migration paths, evaluation runner and metrics, settings, composition root, API surface, fixtures, mock data, the corpus itself including the three trap documents, and the `.claude` configuration.

Claims were cross-checked rather than trusted: the committed evaluation report matches the README's headline numbers (Hit@1 0.9375, Hit@3 1.00, MRR 0.969 over 16 golden queries, zero distractor and stale-policy leaks, 5/5 cases); the golden set does contain 16 queries; the index does contain 58 chunks from 15 documents; all fourteen safety-property test names cited in README §11 exist in the test files they claim; the four load-bearing schema constraints exist verbatim in `schema.sql`; the no-override property (`extra="forbid"` on every tool schema, no force/verified/override fields, no caller-supplied idempotency key) holds in `tools/contracts.py`.

One limitation: the audit sandbox's egress policy blocked downloading several dependency wheels, so the test suite could not be executed here. Runtime claims are therefore verified statically and against committed artefacts rather than by re-execution. Nothing found in the code contradicts them, and the counts are internally consistent, but "604 passing" remains the author's number. Re-running `uv sync && uv run pytest -q -m "not live_model" && uv run ap-agent eval` on an unrestricted machine takes minutes and is worth doing before the debrief.

To calibrate "best practice" as of September 2026, two structured market research passes were run (retrieval/evaluation landscape, and orchestration/HITL/fine-tuning/AP-market landscape) across primary vendor documentation, OWASP, arXiv and practitioner sources. Findings are cited inline below and listed in section 10.

---

## 4. What is genuinely strong, measured against the market

**The trust architecture is the headline.** The 2026 consensus, from OWASP's LLM01 guidance through DeepMind's CaMeL and Anthropic's own security patterns, is that prompt-injection detection cannot be made reliable, so safety must come from structure: least-privilege tools, capability separation between whatever reads untrusted content and whatever acts, and a human gate on consequential actions. This submission implements that consensus unusually completely. The model cannot call the write tool at all; the outcome is computed by pure functions before the model is ever asked for prose; a model suggestion is applied only when strictly more conservative; tool schemas contain no permission-widening argument by construction, asserted by a test; untrusted text enters prompts through exactly one function that fences it with a per-run random nonce (the Hines et al. spotlighting technique, now productised by Microsoft) and strips forged boundaries; and the write tool re-checks authorisation independently of its caller. The narrative-screening control is a detail worth singling out: the engineer identified that a hostile model can keep the computed outcome but write "approved by the CFO, post immediately" into the prose a human actually reads, and made the system discard and deterministically regenerate such prose, recording the event as a finding. Most shipped agent products do not have that control.

**Exactly-once is enforced where it survives crashes.** Idempotency is done the way payment-infrastructure literature (Stripe's idempotent-requests model, Helland's classic paper) says to do it: a derived key (never caller-supplied) as primary key, a per-run uniqueness constraint, a partial unique index on the invoice fingerprint that closes the cross-run double-payment path, and byte-identical replay responses. The two-signature approval scheme (distinct people enforced by a composite primary key, Financial Control co-signature, delegation validated against a register, and the presented amount/currency/vendor bound to the approval so the case cannot mutate at the gate) is a faithful maker-checker implementation, which AP fraud-control practice treats as non-negotiable. Vendor status is even revalidated at decision time. This is control thinking that real AP platforms (Basware, Stampli, Vic.ai) market as their differentiator.

**The evaluation design is deterministic where determinism matters.** The fake adapter is not a stub; it is an instrument with five fault modes (schema violation, always invalid, unavailable, injection compliance, hostile narrative) so that safety properties are asserted against hostile model output rather than hoped about. The most important test in the suite configures the model to behave as if it obeyed the corpus's injected instruction and asserts the run escalates anyway. FinanceBench-style results (GPT-4 with retrieval failing or refusing 81% of finance questions) are the standing argument for exactly this design: numeric and control outcomes must not depend on model quality, and here they provably do not.

**The framework decision is the current market-defensible one, argued the way the brief wanted.** Anthropic's Building Effective Agents and the 12-factor-agents playbook both say to start with explicit code and adopt a framework only for what it uniquely provides; 2026 practitioner sentiment includes a visible move back to raw SDKs for production control flow. ADR-0002 does the comparison honestly (LangGraph named as closest fit, what it would and would not provide, the ~10-phase threshold for adopting it, Temporal-class durable execution named for production) and the design note's framework-versus-code table maps each brief requirement to where it is actually enforced. The criticism that checkpoint-style frameworks do not give true durable execution (auto-detection, auto-resume, duplicate-recovery guards) is current and correct, and the submission neither claims nor needs those properties at this scale.

**Retrieval is right-sized and the reasoning is evidence-backed.** For a 15-document, terminology-dense policy corpus, BM25 with structure-aware section chunking, metadata filtering and status demotion is not a compromise; 2026 finance-domain benchmarking (April 2026 arXiv work on financial-document RAG; the FinMTEB finding that lexical signal is unusually strong in finance text) supports lexical-first at this scale, and the corpus traps are handled by filters and metadata, which no embedding model can infer. The measured corrections (compound-token inflation, query-term deduplication, each tied to a named golden query with before/after numbers) are the kind of retrieval engineering most candidates never show. Hybrid dense retrieval exists behind a flag with RRF, and the recommendations file states the exact condition for turning it on. The two absolute gates (zero distractor leaks, superseded never outranks current) are the right idea: an average that tolerates one leak is not a control.

**Documentation and epistemic honesty are top-percentile.** Known limitations are stated with their failure modes rather than hidden (heuristic injection detection and what evades it, the 17-versus-16 tool budget defect on double-delegated two-signature runs, weekend-only business days, abandon-not-cancel timeouts, no authentication). The recommendations file is ordered by risk-per-effort and reads like a competent architect's production plan: identity provider, edge authentication, Temporal-class durability, Postgres, vector store with permission filtering and deletion propagation, OTel export, review queue. The AI-usage declaration goes beyond disclosure to describe the control loop (five adversarial review passes, four release blockers found, thirty-one stale documentation claims corrected) and the `.claude/` directory makes that process reproducible. The "stored and never read" pattern named at the end of the recommendations file (controls that appear in output but are never enforced) is a genuinely insightful observation about AI-generated systems.

---

## 5. Gap analysis, ranked

Severity reflects impact on the brief's goals and on production readiness, weighted by whether the submission itself already acknowledges the gap. "Declared" means the limitation is stated in the README, design note or recommendations.

### G1. No committed evidence the live-model path was ever exercised (Major, undeclared)

Every committed transcript, the evaluation report and all 604 tests-but-one run on the deterministic fake adapter (`provider: fake, model: fake-deterministic-1` in `docs/samples/eval_report.json`). The single live test is excluded by default, and no live evaluation report, live transcript, token-cost figure or screenshot is committed. The Anthropic adapter is well-built (forced tool use, separated transport and schema retries, strict validation either way), but as evidence it is unexercised code. The architecture deliberately makes model quality non-load-bearing for outcomes, which is the right defence, but the two phases the model does own (evidence synthesis, approver-facing narrative) are the product an approver reads, and their quality on a real model is unmeasured. For an Agentic AI Engineer assessment this is the difference between "the control plane is proven" and "the agent is proven".

*Fix (roughly 30 minutes):* run `ap-agent eval --provider anthropic`, commit the live evaluation report and one live transcript beside the deterministic ones, note the token counts per case, and state any behavioural differences observed.

### G2. Generation quality has no measure at all (Major, partially declared)

Retrieval is measured; generation is only constrained. Citation grounding, schema validity and conservative-only outcomes are enforced deterministically, which prevents the worst failures, but nothing measures whether the narrative is faithful to the evidence, complete, or useful to an approver: no RAGAS-style faithfulness or answer-relevancy scoring, no LLM-as-judge pass (even with the documented reliability caveats), no human-rubric sample. The 2026 norm for a system like this is a small golden set of expected findings per case with a faithfulness gate on the narrative, run in CI on the deterministic tier and periodically against the live model. The recommendations file's own item 9 (contradiction detection between narrative claims and systems of record) is the deterministic half of this; the measurement half is absent.

### G3. The retrieval evaluation cannot yet catch real regressions (Moderate, partially declared)

Sixteen queries over 15 documents, gates set one query's worth below the measured score, all phrased by the author against their own chunking. That is right-sized for the corpus and honestly documented, but it is a smoke test, not a regression harness: no paraphrase or synonym queries (the declared weakness of lexical retrieval is exactly what the set does not probe), no negative queries (questions the corpus cannot answer, where returning nothing is correct), and the shipped hybrid mode has no golden numbers at all, so a code path advertised as the recall upgrade is unproven. Either measure hybrid mode on an extended golden set or cut the flag; shipping an unmeasured alternative retrieval path invites turning it on blind.

### G4. The orchestrator has outgrown its own argument (Moderate, undeclared)

ADR-0002's case for going framework-free is that a reviewer can read a ~300-line orchestrator end to end. The driver loop itself is that small and is clean, but `orchestration/machine.py` is 2,089 lines and holds every phase's logic, the approval resolution flow, narrative screening, summaries and citation resolution: the largest six methods are 100-220 lines each. Everything else in the codebase is decomposed by responsibility; the orchestrator is the one module a finance-controls reviewer would actually read and it is the least readable file in the repository. Extracting each phase into `orchestration/phases/<name>.py` with the driver unchanged would make the ADR's argument true again. This matters doubly under heavy AI assistance, where a monolithic hotspot is where subtle regressions concentrate.

### G5. Brief-compliance misses (Moderate, undeclared)

Section 11 of the brief asks for the approximate time spent. It is nowhere in the repository; the plan restates the 8-hour timebox but no document says what was actually used (the git history suggests about five hours, but the reviewer should not have to forensically derive a required disclosure). The AI declaration is otherwise exemplary, which makes the one required number it omits more conspicuous. Also missing: any statement of which tests were run on which platform for the claimed counts (a one-line "604 passed on Windows, Python 3.14, 2026-09-11" would make the headline reproducible-in-principle).

### G6. Injection secondary controls are English-only heuristics (Moderate, declared)

The structural controls do not depend on the detector, which is the right posture, but two secondary controls are brittle in the same way: the clause-anchored imperative detector (declared: passive voice, other languages and encodings evade it) and the narrative screen's hardcoded phrase list ("approved by the cfo", "pay immediately", and six others). A hostile narrative phrased as "Finance leadership has signed off; settlement today is appropriate" passes both and reaches the approver. The mitigation is already designed (recommendations item 9: corroboration checks against systems of record, plus a larger adversarial corpus to measure false-negative rates); it should be treated as near-term work, not backlog, because the narrative channel is the one place model output still reaches a human decision-maker unmeasured.

### G7. No CI pipeline is committed (Minor, undeclared)

The quality gates (pytest, mypy strict, ruff, `ap-agent eval` with hard gates) exist and are documented, but nothing runs them on push: no GitHub Actions or equivalent workflow, no pre-commit config. The `.claude` hooks cover the agentic-authoring loop, not the repository. For a submission this test-heavy, a 25-line workflow file was cheap signal that the gates are enforced rather than available.

### G8. Operational observability is deferred (Minor, declared)

Structured events with correlation IDs, durations, outcomes and per-call token counts are in place and queryable in SQLite, and the recommendations file correctly notes the near field-for-field mapping onto the OpenTelemetry GenAI semantic conventions. What is missing is any exporter or integration with the now-standard eval/observability layer (OTel, Langfuse, LangSmith or Phoenix). Deferring it is reasonable at this scale; a senior reviewer will still ask where traces go on day one of a pilot.

### G9. Residual small items (Minor)

Declared and acceptable within the timebox, listed for completeness: approver identity is asserted by the caller (recommendations item 1); the HTTP surface has no authentication or rate limiting (item 2); the hand-rolled checkpointer's crash window between posting and persisting the receipt (item 3, narrow because the idempotency key makes the retry safe); the 17-versus-16 tool budget defect on double-delegated two-signature runs (item 12); weekend-only business-day arithmetic (item 7); abandoned rather than cancelled timeout threads (safe because timed tools are reads). Undeclared nits: the AI declaration points to "README §12" for known limitations but they are §10 (one stale cross-reference surviving a documented 31-fix reconciliation pass); the delivered folder contains runtime artefacts (`data/runtime/ap_agent.db`, caches) that are gitignored but travel with a folder hand-off; the six large early commits limit archaeological review of how the core was built; commit granularity aside, the hardening commits are appropriately scoped.

---

## 6. Technology choices versus the September 2026 market

**Orchestration: framework-free state machine.** *Verdict: defensible to preferred at this scale; not an outlier.* The market's centre of gravity for production agentic workflows is LangGraph 1.0 (GA October 2025) when teams want checkpointing, `interrupt()`-based human-in-the-loop and the LangSmith ecosystem; OpenAI Agents SDK, Google ADK and AWS Strands when single-vendor; Temporal or Restate underneath any of them when genuine durable execution is required, because checkpointing alone does not auto-detect failures or guard duplicate recovery. Against that landscape, a hand-written explicit state machine for a 7-phase linear flow is squarely inside Anthropic's own "start simple" guidance and the 12-factor-agents playbook, and the two things a framework would have supplied (pause/resume, checkpointing) are implemented here in ~150 lines over SQLite with the harder parts (approval semantics, exactly-once, fencing, redaction) being application work under any framework. Worth knowing for the debrief: LangGraph's interrupt resumes by re-executing the node, so its adopters need exactly the idempotency discipline this submission built anyway. Had the candidate chosen LangGraph instead, the audit would call that defensible too; the brief rewarded a deliberate, explained choice, and got one.

**Retrieval: BM25 + structure-aware chunking + metadata re-ranking, hybrid behind a flag.** *Verdict: right call for this corpus; one cheap 2026 upgrade left unclaimed.* The 2026 default stack for enterprise document QA at scale is hybrid BM25 + dense with reciprocal rank fusion and a cross-encoder reranker, and finance-domain benchmarking makes the lexical component load-bearing rather than legacy (BM25 alone outperformed dense retrieval on financial documents in April 2026 benchmarking, and FinMTEB found finance semantic tasks unusually kind to lexical signal). At 15 documents and 58 chunks, retrieval quality is saturated (Hit@3 1.00) and the submission's own analysis of why embeddings, vector databases and rerankers are premature is the same analysis this audit's market research produced independently. The one technique that would have been essentially free at this scale and is now standard practice is Anthropic-style contextual chunk annotation (a situating sentence prepended to each chunk before indexing; reported 49% retrieval-failure reduction, 67% with reranking). Section-per-heading chunking with inherited front matter achieves part of the same effect here, so this is an upgrade note, not a defect. Finance-specific embedding models (voyage-finance-2, Fin-E5) become relevant only if the corpus grows and paraphrase queries measurably fail, which is exactly the trigger the recommendations file states.

**Model integration: Anthropic adapter with forced tool use, deterministic fake for tests.** *Verdict: correct pattern; one modernisation available.* Provider-native structured output plus Pydantic validation plus one bounded repair is the textbook 2026 pattern, and keeping provider names out of orchestration honours the brief. Anthropic's native structured outputs (strict schema conformance, now generally available) would slightly simplify and harden the adapter versus forced `tool_choice`; the code as written validates everything anyway, so this is a refinement. The deterministic-adapter-first testing strategy, with fault modes driving the failure paths, matches how mature teams keep frontier-model calls out of CI. The default model name (`claude-sonnet-5`) is current-generation and lives only in configuration, as required. A second adapter (the brief's optional extension) was correctly skipped in favour of core solidity.

**Persistence: SQLite WAL with schema-enforced invariants.** *Verdict: exactly right for the brief; production path documented.* The brief explicitly blesses simple local persistence demonstrating restart/resume and idempotency, and this submission gets more safety out of SQLite (constraints as the guarantee, transactional migrations with `user_version`, refusal to open newer stores) than most teams get out of Postgres. The Postgres migration path (`BEGIN IMMEDIATE` to `SELECT ... FOR UPDATE`, one module contains all SQL) is stated and credible.

**Fine-tuning: not used.** *Verdict: correctly rejected; using it would have been a mistake.* The 2026 decision framework is stable: grounding, freshness and auditable citations demand retrieval; fine-tuning earns its place for format/style at volume, latency, or cost compression, and RAFT-style retrieval-aware tuning only once a working RAG pipeline measurably underperforms. A policy-grounded compliance workflow needs to cite the current policy document, which baked-in weights cannot do, and its business logic (tolerances, duplicate rules) belongs in code, not in weights or prompts. The realistic future fine-tuning candidates for this system are a small extraction model for messy OCR invoices at volume and, later, embedding fine-tuning on the enterprise corpus; both are correctly out of scope, though the design note could have said this in one paragraph to show the option was weighed.

**The market reality check.** Commercial AP automation in 2025-2026 (Coupa's agentic payments, Vic.ai's confidence-thresholded autonomy, Bill/Tipalti/Stampli, Basware's fraud-control acquisitions) still routes the majority of invoices through human review; industry benchmarking puts average touchless processing around 25% with best-in-class near 35%, and every leader keeps human approval on consequential actions. This submission's posture (deterministic matching, confidence stated but never load-bearing, hard human gate, four-eyes for higher risk) is the same control philosophy, implemented more conservatively than some shipping products. As a take-home judged on soundness, that is the right side to err on.

---

## 7. The delta to a 10: what the strongest possible version adds

Everything below fits inside the same timebox philosophy (highest risk reduction per unit effort) and none of it requires new architecture.

1. **Live-path evidence** (G1): one committed live evaluation report and transcript with token costs. Turns the strongest claim in the README from asserted to shown.
2. **A generation-quality gate** (G2): per-case expected findings, a deterministic contradiction check of narrative against systems of record, and a faithfulness score (RAGAS-style or a caveated judge) reported beside the retrieval metrics.
3. **Orchestrator decomposition** (G4): phases as modules, driver unchanged; the ADR's readability claim becomes literally true.
4. **A hardened golden set** (G3): paraphrase queries, negative queries, and hybrid-mode numbers, so the flag is a measured option instead of a bet.
5. **A CI workflow** (G7): pytest, mypy, ruff and `ap-agent eval` on push; roughly 25 lines for a large credibility gain.
6. **The two disclosure lines** (G5): actual hours spent, and the platform on which the headline counts were produced.
7. **Adversarial breadth** (G6): five to ten injection variants (passive voice, non-English, encoded, split across attachments) with the detector's false-negative rate reported, which the submission's own recommendations already argue for.

None of these is exotic; their absence is what separates "exceptional take-home" from "reference implementation".

---

## 8. Debrief probes: testing that the engineer owns the design

The brief's §11 makes the live defence part of the assessment, and the declared AI assistance makes it the decisive part. These questions have crisp answers that exist in the submission; hesitation on several of them would materially change the rating.

1. In `AWAITING_APPROVAL`, a second callback from the same approver and a first signature from a second approver are both self-transitions. Which one spends a tool call, why, and what does the other return? (Tests: two-signature design, replay semantics; the answer involves the delegation lookup and the stored-response replay.)
2. The process dies after `submit_finance_decision` writes the decision row but before the run state is saved. What does the user see, what does resume do, and which constraint makes the retry safe? (Tests: the crash window their own recommendations item 3 names.)
3. Why is `invoice_fingerprint` a partial unique index excluding empty strings, and what would break if it were a plain unique column? (Tests: whether the cross-run double-payment fix is understood or transcribed.)
4. Golden query Q09 regressed before the tokenizer change. Walk through exactly how emitting a hyphenated compound both whole and split inflates BM25, and why identifiers keep the whole form. (Tests: the deepest retrieval work in the repo.)
5. Why demote the superseded authority matrix to 0.3 of its score instead of filtering it out, and construct a query where 0.3 is the wrong number. (Tests: the metadata re-ranking design and its limits.)
6. Which exact run shape exhausts the 16-attempt tool budget at the seventeenth attempt, and what does the user observe when it happens? (Tests: the documented budget defect.)
7. A tool responds with data that fails its output schema. Why is that treated as permanent rather than transient, and what did the earlier `from_attributes=True` bug let through? (Tests: the validation-strictness story in `tools/base.py`.)
8. If this were rebuilt on LangGraph, what do `interrupt()` and the checkpointer give you, what do they not give you, and where does node re-execution on resume interact with the decision tool? (Tests: whether ADR-0002 is understood from both sides.)
9. Your narrative screen is a phrase list. Give an attacker sentence that passes it and reaches the approver, then describe the non-pattern-matching fix. (Tests: honesty about G6 and the corroboration-check design.)
10. Why does `ToolCallResult[output_model]` need a `type: ignore[valid-type]` under mypy strict, and what alternative typing did you reject? (Tests: whether the PEP 695 generics are theirs.)
11. FIN-002 ends in `AWAITING_APPROVAL` with outcome `REJECT_DUPLICATE`. Why does a rejection need an approval gate at all? (Tests: understanding that recording a rejection is itself a consequential ledger action.)
12. What would you delete from this submission if asked to halve its size without losing a safety property? (Tests: judgement about the 24,000 lines; strong answers name the rules breadth or documentation volume, not the controls.)

---

## 9. On velocity, AI assistance and authenticity

The submission is transparent that AI coding assistance was used across the whole of the work, and the brief permits exactly that. Two observations belong in an audit rather than being left implicit. First, the throughput (about 24,000 lines of source, tests and documentation, 13 commits, in roughly five hours of wall clock) is achievable only with heavy agentic generation; the declaration says so and describes a disciplined control loop around it, and the repository contains the actual review-agent and hook configuration, which is more verifiable than any prose claim. Second, the artefact therefore measures the engineer's ability to *direct, constrain and audit* AI-produced systems, which is arguably the job description for an Agentic AI Engineer in 2026, and it measures individual unaided coding not at all. The distinction between those two skills is real, it is what section 8 exists to resolve, and the submission itself invites the scrutiny ("be prepared to explain every decision") rather than obscuring it. The declared review process also left fingerprints that corroborate it: the hardening commits, the recommendations file's "delivered since first release" table, and the recorded defects (an indicator-attribution bug, a connection-sharing concurrency bug, migration crash windows) are the kind of specific, unflattering detail that fabricated process narratives do not usually include.

One fairness note in the other direction: velocity is not itself a red flag, but it sets the interview bar. A candidate who can answer section 8 fluently has demonstrated senior-to-staff systems judgement twice over (once in the artefact, once live). A candidate who cannot has demonstrated prompt operation, and the rating below assumes the former until the debrief says otherwise.

---

## 10. Final rating

| Dimension | Score /10 |
|---|---:|
| Agent and RAG architecture | 9.5 |
| Reliability | 9.0 |
| Safety | 9.0 |
| RAG and evaluation | 7.5 |
| Engineering quality and communication | 8.5 |
| **Weighted overall (25/20/20/15/20)** | **8.8** |

**Calibration:** against the take-home submissions this brief will typically attract, a mid-level submission implements the happy path with an agent loop and a vector store and hand-waves idempotency; a senior submission gets the approval gate, typed contracts and some tests right. This submission is beyond both: the differentiated material is the schema-enforced exactly-once design, the structural injection posture with a screened narrative channel, the deterministic evaluation strategy, and decision records that anticipate almost every objection a reviewer could raise, including most of this audit's. The gaps that keep it off a 9.5 are the missing live-model evidence, the unmeasured generation channel, the small self-referential golden set, the orchestrator monolith and the absent CI, and one required disclosure (time spent) left out.

**Recommendation to the reviewer:** treat the artefact as passing at a strong senior level, run the suite once on an unrestricted machine to convert the claimed counts into observed ones, and let the section 8 debrief decide between "senior" and "staff-adjacent". If the debrief holds up, the remaining work items (G1, G2, G5) are a day of effort and the candidate has already written the correct production roadmap themselves.

---

## 11. Sources

**Repository evidence (primary):** `README.md`; `docs/DESIGN_NOTE.md`; `docs/adr/0001-0007`; `docs/RECOMMENDATIONS.md`; `docs/AI_USAGE_DECLARATION.md`; `docs/references.md`; `docs/samples/eval_report.json` and transcripts; `src/ap_agent/` (orchestration/machine.py, orchestration/gates.py, rag/index.py, rag/retriever.py, llm/prompts.py, llm/anthropic_client.py, llm/fake_client.py, tools/base.py, tools/contracts.py, persistence/schema.sql, persistence/repository.py, evaluation/retrieval.py, config/settings.py, composition.py); `tests/` (unit, contract, eval tiers); `fixtures/`; `finance_rag_corpus/` corpus including ADV-001, ADV-002, FIN-POL-003-OLD; `.claude/` configuration; git reflog (13 commits, 11:22-15:15 AEST 2026-09-11).

**Market references (September 2026 research pass):**
- Anthropic, Building Effective Agents (Dec 2024): https://www.anthropic.com/engineering/building-effective-agents
- Anthropic, Contextual Retrieval (Sep 2024): https://www.anthropic.com/engineering/contextual-retrieval
- HumanLayer, 12-Factor Agents: https://github.com/humanlayer/12-factor-agents
- LangChain and LangGraph 1.0 GA announcement (Oct 2025): https://www.langchain.com/blog/langchain-langgraph-1dot0 and interrupt/resume docs: https://docs.langchain.com/oss/python/langgraph/interrupts
- Diagrid, why checkpointing is not durable execution (2026): https://www.diagrid.io/blog/checkpoints-are-not-durable-execution-why-langgraph-crewai-google-adk-and-others-fall-short-for-production-agent-workflows
- OWASP LLM01:2025 Prompt Injection: https://genai.owasp.org/llmrisk/llm01-prompt-injection/
- Hines et al., Spotlighting (arXiv 2403.14720) and Microsoft Azure spotlighting productisation (2025): https://techcommunity.microsoft.com/blog/azure-ai-foundry-blog/better-detecting-cross-prompt-injection-attacks-introducing-spotlighting-in-azur/4458404
- OpenAI, The Instruction Hierarchy (2024): https://openai.com/index/the-instruction-hierarchy/
- Debenedetti et al., CaMeL: Defeating Prompt Injections by Design (2025): https://arxiv.org/pdf/2503.18813
- Financial-document RAG benchmarking, BM25 vs dense vs hybrid+rerank (Apr 2026): https://arxiv.org/html/2604.01733v1
- FinMTEB benchmark and Fin-E5 (Feb 2025): https://arxiv.org/abs/2502.10990
- Voyage AI, voyage-finance-2 (Jun 2024): https://blog.voyageai.com/2024/06/03/domain-specific-embeddings-finance-edition-voyage-finance-2/
- FinanceBench (2023): https://arxiv.org/abs/2311.11944
- RAGAS metrics: https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/ and RAG evaluation guide (2026): https://www.confident-ai.com/knowledge-base/guides/rag-evaluation
- LLM-judge reliability caveats (Jun 2026): https://arxiv.org/html/2606.19544v1
- RAG vs fine-tuning decision framework (2026): https://winder.ai/rag-vs-fine-tuning-2026-decision-framework/ and RAFT: https://arxiv.org/abs/2403.10131
- OpenTelemetry GenAI observability (2026): https://opentelemetry.io/blog/2026/genai-observability/
- AP automation touchless-rate benchmarks (Ardent Partners 2025, via 2026 compilation): https://stealthagents.com/research/ai-invoice-processing-automation-statistics-2026
- Coupa agentic payments update (Aug 2026): https://www.northamericaoutlookmag.com/supply-chain/coupa-expands-agentic-ai-across-procurement-sourcing-and-payments-in-product-update
- Vic.ai autonomy with confidence thresholds (2026): https://www.vic.ai/blog/q1-2026-product-release-expanding-autonomy-across-the-ap-lifecycle
- Stripe, Idempotent requests: https://docs.stripe.com/api/idempotent_requests
- Segregation of duties in AP (2026): https://www.stampli.com/blog/accounts-payable-fraud/segregation-of-duties/

*Prepared as an independent audit. No code, test, fixture or document in the submission was modified.*
