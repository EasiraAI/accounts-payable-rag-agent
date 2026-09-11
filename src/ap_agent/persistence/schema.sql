-- Schema for the accounts-payable agent's local store.
--
-- Five tables: the run's current state, the append-only audit log, human approvals, the
-- signatures collected on each approval, and effective finance decisions.
--
-- The constraints are the interesting part. Four of them are load-bearing safety properties
-- rather than hygiene, and three were added or corrected after a review defeated an earlier
-- version:
--
--   decisions.idempotency_key PRIMARY KEY
--       A duplicated approval callback computes the same key and collides. The collision is
--       how a replay is detected, and detecting it in the database rather than in
--       application code is what makes it survive a crash between "check" and "write".
--
--   decisions.run_id UNIQUE
--       A run has at most one effective decision, for its whole lifetime. Even a caller
--       that invented a fresh idempotency key cannot record a second posting against the
--       same run.
--
--   decisions.invoice_fingerprint UNIQUE (partial)
--       One decision per *invoice*, across runs. Per-run uniqueness left the double-payment
--       path open: three identical invoices submitted as three runs posted three times.
--
--   approval_signatures PRIMARY KEY (approval_id, approver_id)
--       One signature per person per approval, which is what makes a two-signature
--       requirement need two people rather than one person twice.
--
-- Together these make "exactly once" a property of the store rather than of the
-- orchestrator's control flow, which is the only version of the guarantee that survives a
-- crash between a check and a write.
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

CREATE TABLE IF NOT EXISTS approval_signatures (
    approval_id               TEXT    NOT NULL REFERENCES approvals (approval_id) ON DELETE CASCADE,
    approver_id               TEXT    NOT NULL,
    approver_role             TEXT    NOT NULL,
    effective_role            TEXT    NOT NULL DEFAULT '',
    applicable_limit          TEXT,
    authority_register_version TEXT   NOT NULL DEFAULT '',
    delegation_applied        TEXT,
    is_financial_control      INTEGER NOT NULL DEFAULT 0,
    signed_at                 TEXT    NOT NULL,
    comment                   TEXT    NOT NULL DEFAULT '',
    -- One signature per person per approval. This is what makes a duplicate callback from
    -- the same approver a replay, and what stops a two-signature requirement being met by
    -- one person signing twice: a second signature from the same person satisfies nothing
    -- the first did not, and FIN-POL-003 §3's "two approvals" plainly means two people.
    PRIMARY KEY (approval_id, approver_id)
);

CREATE INDEX IF NOT EXISTS idx_signatures_approval ON approval_signatures (approval_id);

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
    created_at      TEXT NOT NULL,
    -- The invoice fingerprint, from the same four fields FIN-POL-005 §1 uses for exact
    -- duplicate matching: vendor, normalised invoice number, currency and gross amount.
    --
    -- This column and its unique index close the gap that per-run uniqueness leaves open. A
    -- security review submitted one identical invoice as three separate runs, approved each
    -- once, and got three posted decisions: `run_id UNIQUE` guarantees one decision per run,
    -- and nothing guaranteed one decision per *invoice*. In an accounts-payable system that
    -- is the double-payment path, and it is the exact behaviour FIN-POL-005 exists to
    -- prevent.
    --
    -- Enforced here as well as by the duplicate-detection rule, because the rule reads an
    -- upstream history service that does not know about decisions this system recorded
    -- moments ago. A constraint does.
    invoice_fingerprint TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_decisions_case ON decisions (case_id);

-- Partial index: the empty default is excluded so historical rows written before the column
-- existed do not collide with each other.
CREATE UNIQUE INDEX IF NOT EXISTS idx_decisions_invoice_fingerprint
    ON decisions (invoice_fingerprint)
    WHERE invoice_fingerprint <> '';
