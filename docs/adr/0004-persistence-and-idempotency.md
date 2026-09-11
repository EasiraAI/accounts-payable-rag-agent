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

SQLite with five tables:

| Table | Purpose | Key |
|---|---|---|
| `runs` | current `RunState` as JSON, status, phase, timestamps, version counter | `run_id` |
| `events` | append-only audit log: `event_type`, `payload_json` (redacted), `duration_ms`, `outcome`, `correlation_id` | autoincrement, indexed by `run_id` |
| `approvals` | pending and resolved approval requests: `approval_id`, `run_id`, `status`, `decided_by`, `decided_at` | `approval_id` |
| `approval_signatures` | one row per person per approval: effective role, applicable limit, register version, whether a delegation was applied | `(approval_id, approver_id)` |
| `decisions` | effective finance decisions: `idempotency_key`, `run_id`, `outcome`, `request_hash`, `response_json`, `invoice_fingerprint` | `idempotency_key` PRIMARY KEY; `run_id` UNIQUE; partial UNIQUE on `invoice_fingerprint` |

Idempotency protocol for `submit_finance_decision`:

1. Key = `sha256(run_id | approval_id | outcome | amount | currency | vendor_id)`, computed in
   the repository, never accepted from the caller. The last three joined the material after a
   review changed the amount at the approval gate: the callback computed the same key as the
   approved decision, collided with it, and was reported as a replay of a decision that had
   never been taken.
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

## Later records that extend this one

- [ADR-0006](0006-schema-versioning-and-migration.md) adds schema versioning and forward
  migration. It follows from this record's central claim: if the constraints *are* the
  guarantee, then the version of the schema is safety-relevant state.
- [ADR-0007](0007-two-signature-approvals.md) adds `approval_signatures`, and makes its primary
  key carry both the FIN-POL-003 §3 two-signature requirement and the replay detection for a
  duplicated callback — for the same reason this record gives for the decision table.

## Consequences

- A `Repository` class is the only module that touches SQL; the orchestrator sees typed
  methods (`save_run`, `append_event`, `create_approval`, `add_approval_signature`,
  `reject_approval`, `record_decision`), plus `schema_version` and `column_names` for
  operational checks.
- Restart test: run to `AWAITING_APPROVAL`, construct a new process, call approve, assert
  completion with identical event history plus new events.
- Migration to PostgreSQL requires changing connection handling and `BEGIN IMMEDIATE` to
  `SELECT ... FOR UPDATE`; the schema is otherwise portable.
