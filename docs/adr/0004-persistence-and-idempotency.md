# ADR-0004: Persistence, resume and idempotency

**Status:** Accepted. **Date:** 2026-09-11.

## Context

The brief requires local persistence that demonstrably supports restart/resume and
idempotency, an audit event log with timestamps, run ID, outcome and duration, and
replay-safe handling of a duplicated approval callback (FIN-005). FIN-POL-007 §5 requires
resuming from the failed control while retaining the prior result.

## Options considered

| Option | Resume | Idempotency | Setup | Notes |
|---|---|---|---|---|
| **SQLite, single file, WAL mode** | Load last `RunState` row. | `UNIQUE` constraint on idempotency key; `INSERT OR IGNORE` then read back. | None. | Standard library. |
| JSON files per run | Trivial to read. | No atomic uniqueness across processes. | None. | Fails FIN-005 under concurrency. |
| PostgreSQL | Same model as SQLite. | Same, plus advisory locks. | Docker service. | Production choice; overkill locally. |
| Redis | Fast. | `SET NX`. | Service. | Not durable by default. |

## Decision

SQLite with four tables:

| Table | Purpose | Key |
|---|---|---|
| `runs` | current `RunState` as JSON, status, phase, timestamps, version counter | `run_id` |
| `events` | append-only audit log: `event_type`, `payload_json` (redacted), `duration_ms`, `outcome`, `correlation_id` | autoincrement, indexed by `run_id` |
| `approvals` | pending and resolved approval requests: `approval_id`, `run_id`, `status`, `decided_by`, `decided_at` | `approval_id` |
| `decisions` | effective finance decisions: `idempotency_key`, `run_id`, `outcome`, `decision_ref`, `request_hash`, `response_json` | `idempotency_key` UNIQUE |

Idempotency protocol for `submit_finance_decision`:

1. Key = `sha256(run_id + approval_id + outcome)`, computed in orchestration code, never
   accepted from the caller.
2. `BEGIN IMMEDIATE`; `INSERT OR IGNORE INTO decisions`; if `changes() == 0`, read the
   existing row, verify `request_hash` matches (mismatch is a hard error, not a replay),
   return the stored response with `replayed=true`; else call the simulated posting
   adapter, store the response, `COMMIT`.
3. `BEGIN IMMEDIATE` serialises concurrent callbacks at the database level, so two
   simultaneous approvals produce one adapter call without application-level locks.

Optimistic concurrency on `runs.version` prevents two workers advancing the same run.

## Rationale

Uniqueness enforced by the database is the only idempotency mechanism that survives a
process crash between "check" and "write". The pattern follows Helland's treatment of
idempotent messaging and the Stripe idempotency-key convention (same key, same request
hash, same response). SQLite in WAL mode is durable, atomic and needs no service, which
matches the local-environment choice.

## Consequences

- A `Repository` class is the only module that touches SQL; the orchestrator sees typed
  methods (`save_state`, `append_event`, `create_approval`, `record_decision`).
- Restart test: run to `AWAITING_APPROVAL`, construct a new process, call approve, assert
  completion with identical event history plus new events.
- Migration to PostgreSQL requires changing connection handling and `BEGIN IMMEDIATE` to
  `SELECT ... FOR UPDATE`; the schema is otherwise portable.
