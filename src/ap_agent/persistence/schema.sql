-- Schema for the accounts-payable agent's local store.
--
-- Four tables, one per durable concern: the run's current state, the append-only audit
-- log, human approvals, and effective finance decisions.
--
-- The constraints are the interesting part. Two of them are load-bearing safety properties
-- rather than hygiene:
--
--   decisions.idempotency_key PRIMARY KEY
--       A duplicated approval callback computes the same key and collides. The collision is
--       how a replay is detected, and detecting it in the database rather than in
--       application code is what makes it survive a crash between "check" and "write".
--
--   decisions.run_id UNIQUE
--       A run has at most one effective decision, for its whole lifetime. Even a caller
--       that invented a fresh idempotency key cannot record a second posting against the
--       same run. This is the constraint that makes "exactly once" a property of the store
--       and not of the orchestrator's control flow.
--
-- Timestamps are ISO-8601 strings in UTC. SQLite has no native date type, and storing text
-- keeps the rows readable during an audit without a decoding step.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA synchronous = FULL;

CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT    PRIMARY KEY,
    case_id     TEXT    NOT NULL,
    status      TEXT    NOT NULL,
    phase       TEXT    NOT NULL,
    -- Incremented on every write. A writer supplies the version it read, and an update
    -- whose version no longer matches is rejected, so two workers cannot both advance one
    -- run from the same starting point.
    version     INTEGER NOT NULL,
    state_json  TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runs_case ON runs (case_id);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs (status);

CREATE TABLE IF NOT EXISTS events (
    event_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT    NOT NULL REFERENCES runs (run_id) ON DELETE CASCADE,
    -- Per-run ordinal. The autoincrement identifier orders events globally; this one orders
    -- them within a run, which is what a reader of a single run's history needs.
    sequence       INTEGER NOT NULL,
    event_type     TEXT    NOT NULL,
    phase          TEXT,
    outcome        TEXT,
    duration_ms    INTEGER,
    correlation_id TEXT    NOT NULL,
    -- Redacted before it is written. There is one egress path, observability.events, and it
    -- applies observability.redact to every payload.
    payload_json   TEXT    NOT NULL,
    created_at     TEXT    NOT NULL,
    UNIQUE (run_id, sequence)
);

CREATE INDEX IF NOT EXISTS idx_events_run ON events (run_id, sequence);
CREATE INDEX IF NOT EXISTS idx_events_type ON events (event_type);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id      TEXT PRIMARY KEY,
    run_id           TEXT NOT NULL REFERENCES runs (run_id) ON DELETE CASCADE,
    case_id          TEXT NOT NULL,
    status           TEXT NOT NULL,
    -- The full ApprovalRequest, including what was presented to the approver. FIN-POL-003
    -- §5 requires the record to show the approver saw the amount, vendor, exceptions and
    -- citations before deciding, so what was shown is stored, not only what was decided.
    request_json     TEXT NOT NULL,
    decided_at       TEXT,
    decided_by       TEXT,
    decided_by_role  TEXT,
    decision_comment TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_approvals_run ON approvals (run_id, status);

CREATE TABLE IF NOT EXISTS decisions (
    idempotency_key TEXT NOT NULL PRIMARY KEY,
    run_id          TEXT NOT NULL UNIQUE REFERENCES runs (run_id) ON DELETE CASCADE,
    case_id         TEXT NOT NULL,
    approval_id     TEXT NOT NULL,
    outcome         TEXT NOT NULL,
    -- Hash of the decision arguments. An identical key with a different hash is a caller
    -- error, not a replay: returning the stored response would answer a different question
    -- from the one asked.
    request_hash    TEXT NOT NULL,
    response_json   TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_decisions_case ON decisions (case_id);
