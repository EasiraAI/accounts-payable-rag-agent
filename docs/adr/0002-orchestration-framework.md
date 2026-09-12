# ADR-0002: Orchestration approach

**Status:** Accepted. **Date:** 2026-09-11.

## Context

The brief requires a bounded agent loop or explicit state graph with a step budget, a hard
stop at approval, safe resume after restart, and a clear statement of what the framework
provides versus what the code enforces. Reviewers will assess "clear states, bounded
decisions, understandable control flow".

## Options considered

### A. Explicit state machine in plain Python (chosen)

A `RunState` pydantic model, an enum of phases, and a `step()` function per phase. A
driver loop advances phases until `AWAITING_APPROVAL`, `COMPLETED` or `FAILED`, persisting
state after every step. The LLM is called from exactly two phases (evidence synthesis and
recommendation narrative) through a typed adapter.

- Provides: nothing hidden. Every transition, budget check and gate is readable in one file.
- Enforces in code: step budget, tool budget, approval gate, idempotency, output validation.
- Cost: we write the checkpointing and resume ourselves (about 150 lines with SQLite).

### B. LangGraph `StateGraph` with `interrupt()` and a SQLite checkpointer

LangGraph models the same phases as nodes, gives `interrupt_before` for human-in-the-loop,
and a `SqliteSaver` checkpointer for resume. It is the closest off-the-shelf match to the
brief's "explicit state graph" wording.

- Provides: graph runtime, checkpointing, interrupt/resume, streaming, tracing hooks.
- Still must be enforced in code: deterministic reconciliation, prompt fencing,
  idempotent decision tool, redaction, output validation. The framework does not know what
  an approval is; it only pauses.
- Cost: a large dependency tree (langchain-core, langgraph, langgraph-checkpoint-sqlite);
  version churn; the reviewer must know LangGraph to audit the control flow; the interrupt
  model resumes by re-executing the node, which makes exactly-once semantics for the
  decision tool something we must still build ourselves.

### C. Provider-native agent loop (Anthropic tool-runner style)

Hand the model the tool list and let it drive until it stops. Simplest to write.

- Rejected: control flow lives in the model, which contradicts "bounded decisions" and
  makes the approval gate a prompt instruction rather than a structural stop. The brief
  explicitly asks for tools to be called "only when needed, not indiscriminately", which
  is a property of a planner, not of a free tool loop.

### D. Durable-execution engines (Temporal, Restate, Vercel Workflow)

Give exactly-once activities and durable timers, which is the right production answer
for resume and idempotency.

- Rejected for the timebox: they require a server component or a hosted account, which
  breaks the "runs on a laptop in one command" requirement. Named in the design note as the
  production change.

## Decision

Option A: a framework-free explicit state machine with SQLite checkpointing. LangGraph is
recorded as the framework we would adopt if the graph grew beyond roughly ten phases or
needed streaming, and Temporal-class durable execution as the production substitute for
the hand-written checkpointer.

## Rationale

The assessment weights safety and reliability above breadth. Every property the reviewer
will probe (budget, gate, idempotency, resume, injection resistance) is something the
framework options still leave to us. Given that, the framework adds audit surface without
removing work. A 300-line orchestrator that a finance-controls reviewer can read end to end
is a better demonstration than a graph definition plus a dependency they must trust.

## Consequences

- **The readability claim, kept honest.** This record's case for going framework-free is that a
  reviewer can read the orchestration end to end. By the time the controls were complete,
  `machine.py` had reached 2,154 lines and an audit was right that the argument no longer held.
  It is now 722, with the seven phase handlers in `orchestration/phases/` and the approval
  semantics, narrative screening, citations and summaries in modules of their own.

  The part worth recording is why that took a protocol rather than a move. Each phase reached
  into the orchestrator's privates, so extracting them without one would have handed every
  module the orchestrator itself: a rename, not a decomposition. `phase_context.py` declares
  four members, and a phase that receives only a context can reach only a clock, a repository,
  a retriever and a model call. What a phase is capable of is now legible from its signature,
  which is the same argument this system makes about bounded tools.

  `_resolve` stays in `machine.py` at 192 lines. It calls the driver, the emitter and the
  finaliser, so it is orchestration rather than a phase; the question a controls reviewer opens
  it for, "may this person sign?", is a named function in `approvals.py`.

- `src/ap_agent/orchestration/` holds `phases/` (the phase plan, its preconditions and the seven
  handlers),
  `machine.py` (driver and transitions) and `gates.py` (budget and approval gate). The
  run-state model lives in `domain/run_state.py` rather than in this package: it is a typed
  contract that the API, the CLI and the persistence layer all read, so keeping it beside the
  driver would have made three consumers depend on the orchestrator to describe a run.
- Phases: `INTAKE -> RETRIEVE_POLICY -> GATHER_EVIDENCE -> RECONCILE -> ASSESS_RISK ->
  RECOMMEND -> AWAITING_APPROVAL -> EXECUTE_DECISION -> COMPLETED`, with `HELD` and
  `FAILED` as terminal alternatives from any phase.
- Resume loads the last persisted `RunState` and re-enters the driver at the stored phase.
  Every phase is written to be safe to re-run (reads are repeatable; the only write, the
  decision, is idempotent).
