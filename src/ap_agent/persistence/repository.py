"""SQLite repository.

The only module in the codebase that contains SQL. Everything above it sees typed methods,
which is what lets the store be swapped for PostgreSQL without touching orchestration: the
changes would be the connection handling and ``BEGIN IMMEDIATE`` becoming
``SELECT ... FOR UPDATE``.

## Why the database enforces idempotency

The obvious implementation of "record this decision once" is to look for an existing record
and write one if none is found. That is wrong under concurrency and under crashes, because
between the read and the write another caller can write, or this process can die having
already called the posting adapter. Either way the invariant that matters, exactly one
posting per approved run, is lost.

So the uniqueness is declared in the schema and the collision is the detection mechanism.
``INSERT`` either succeeds, in which case this caller is the first and proceeds to the
adapter, or it raises ``IntegrityError``, in which case a decision already exists and the
stored response is returned. ``BEGIN IMMEDIATE`` takes the write lock up front so two
concurrent callers serialise at the database rather than racing in application code.

## Why optimistic concurrency on runs

A run is advanced by whichever process holds it. Rather than locking a run for the duration
of a phase, which would strand it if that process died, each write asserts the version it
read. A stale write is rejected and the caller reloads. The failure mode is a retry, not a
stuck run.

## Two layers of write serialisation, and why both are needed

One instance holds one connection, shared across threads. SQLite permits that with
``check_same_thread=False``, but a connection has a single transaction context, so two
threads issuing ``BEGIN IMMEDIATE`` on it collide with "cannot start a transaction within a
transaction". That was not a theoretical concern: a concurrent-append test hit it on eight of
twelve threads.

Writes through one instance are therefore serialised by a process-local lock. Writes from
*separate* connections, which is what a second process or a connection-per-worker deployment
looks like, are serialised by SQLite's own write lock and made safe by the ``UNIQUE``
constraints.

The distinction matters because only the second layer survives a crash. The lock stops two
threads interleaving; the constraints stop two processes double-posting. The exactly-once
guarantee rests on the constraints, and the concurrency test that proves it deliberately uses
one connection per thread so it exercises that path rather than the lock.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, NamedTuple

from pydantic import BaseModel, ConfigDict

from ap_agent.domain.enums import ApprovalStatus, Outcome, RunStatus
from ap_agent.domain.errors import ApprovalStateConflict, RunNotFound
from ap_agent.domain.evidence import normalise_invoice_reference
from ap_agent.domain.results import ApprovalRequest, ApprovalSignature, DecisionReceipt
from ap_agent.domain.run_state import RunState

_SCHEMA_PATH: Final = Path(__file__).with_name("schema.sql")

#: SQLite busy timeout. Long enough to ride out a concurrent writer on a local file,
#: short enough that a genuine deadlock surfaces rather than hanging a request.
_BUSY_TIMEOUT_SECONDS: Final = 5.0

# Shape of the store that this version of the code expects, recorded in SQLite's
# ``user_version`` pragma.
#
# It exists because every CREATE in schema.sql is ``IF NOT EXISTS``, which is right for a
# fresh database and silently wrong for an existing one: a table that is already present
# keeps whatever columns it was created with, and the statement reports success. Adding
# ``decisions.invoice_fingerprint`` demonstrated the failure mode. New code read a column
# that an existing store did not have, the CREATE was a no-op, and the first query against
# the running database raised "no such column" at startup rather than at deploy time.
#
# So the version is checked, and an older store is migrated forward explicitly. A store
# newer than the code is refused outright: a rollback that keeps running against a
# forward-migrated database is how a constraint gets quietly dropped from the enforcement
# path, and for this store the constraints *are* the exactly-once guarantee.
SCHEMA_VERSION: Final = 2

#: What ``_store_version`` reports for a database with no tables. An unstamped store reports
#: the same zero, so the two are told apart by whether any table exists.
_UNVERSIONED: Final = 0


class _AddColumn(NamedTuple):
    """One migration step: add a column to a table, if it is not already there.

    Guarded rather than unconditional, and the guard is not defensiveness. A review
    demonstrated the failure. An ``ALTER TABLE`` that commits before the version stamp leaves
    a store whose shape is version 2 and whose recorded version is still 1; re-running the
    bare ALTER on the next open raises "duplicate column name" every time, and the store
    becomes permanently unopenable. Stamping inside the same transaction closes that window,
    and a step that checks before acting means a store which slipped through it under an
    earlier build, or was patched by hand, still opens.

    The same guard covers the other order of failure. A first run interrupted partway through
    ``schema.sql`` can leave ``runs`` present and ``decisions`` absent, which reads as a legacy
    store. There is then no table to alter, the step is skipped, and the script creates the
    table complete.
    """

    table: str
    column: str
    statement: str


# Steps that carry a store from version N-1 to N, keyed by the version they produce. Written
# by hand rather than derived from schema.sql, because a migration has to say what happens to
# rows that already exist and a CREATE statement cannot express that. The default below is the
# value the partial unique index excludes, so decisions recorded before the column existed do
# not collide with each other.
_MIGRATIONS: Final[dict[int, tuple[_AddColumn, ...]]] = {
    2: (
        _AddColumn(
            table="decisions",
            column="invoice_fingerprint",
            statement=(
                "ALTER TABLE decisions ADD COLUMN invoice_fingerprint TEXT NOT NULL DEFAULT ''"
            ),
        ),
    ),
}


class StaleRunVersion(ApprovalStateConflict):
    """A run was written by someone else since this caller read it."""

    def __init__(self, run_id: str, expected: int, actual: int | None) -> None:
        super().__init__(
            f"run {run_id} is at version {actual}, not the expected {expected}; "
            "reload the run and retry"
        )
        self.run_id = run_id
        self.expected = expected
        self.actual = actual


class StoredEvent(BaseModel):
    """One audit event as stored."""

    model_config = ConfigDict(extra="forbid")

    event_id: int
    run_id: str
    sequence: int
    event_type: str
    phase: str | None
    outcome: str | None
    duration_ms: int | None
    correlation_id: str
    payload: dict[str, Any]
    created_at: datetime


def compute_idempotency_key(
    *,
    run_id: str,
    approval_id: str,
    outcome: Outcome,
    amount: Decimal,
    currency: str,
    vendor_id: str,
) -> str:
    """Derive the idempotency key for a finance decision.

    Computed here from values the system controls, never accepted from a caller. A
    caller-supplied key would let a client defeat the guarantee by varying it, which is the
    opposite of what an idempotency key is for.

    The amount, currency and vendor are part of the key material, not only the run and the
    approval. A security review parked a run at the approval gate, mutated the request
    amount, and watched the inflated figure post under the original approval: the key did not
    depend on the amount, so the altered decision looked like the same decision. Folding the
    monetary facts in means a changed amount produces a different key, which the ``run_id``
    uniqueness constraint then refuses outright.
    """
    material = f"{run_id}|{approval_id}|{outcome.value}|{amount}|{currency}|{vendor_id}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def compute_invoice_fingerprint(
    *, vendor_id: str, invoice_reference: str, currency: str, gross_amount: Decimal
) -> str:
    """Derive the invoice fingerprint used to prevent a second posting of one invoice.

    The four fields are exactly those FIN-POL-005 §1 names for exact duplicate matching:
    vendor identifier, normalised invoice number, currency and gross amount. The number goes
    through ``normalise_invoice_reference``, the same function the duplicate rule uses, so the
    constraint and the rule cannot disagree about what counts as the same invoice. They used
    to agree by having identical copies of the same comprehension.

    This exists because per-run uniqueness was not enough. A review submitted one identical
    invoice as three separate runs, approved each once, and received three posted decisions.
    The duplicate-detection rule could not have caught it: it reads an upstream history
    service that knows nothing about decisions this system recorded seconds earlier.
    """
    normalised = normalise_invoice_reference(invoice_reference)
    material = f"{vendor_id}|{normalised}|{currency.upper()}|{gross_amount}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def compute_request_hash(payload: dict[str, Any]) -> str:
    """Stable hash of decision arguments, used to distinguish a replay from a key collision."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


class Repository:
    """Durable store for runs, events, approvals and decisions."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            db_path,
            timeout=_BUSY_TIMEOUT_SECONDS,
            isolation_level=None,  # explicit transaction control
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        # Serialises write transactions issued through this instance. See the module
        # docstring: a single connection has one transaction context, so concurrent
        # BEGIN IMMEDIATE statements on it are an error rather than contention.
        self._write_lock = threading.Lock()
        self._apply_schema()

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def schema_version(self) -> int:
        """The schema version of the open store. Read after construction it equals
        ``SCHEMA_VERSION``, so it is a useful thing for an operator to assert on."""
        return int(self._connection.execute("PRAGMA user_version").fetchone()[0])

    def column_names(self, table: str) -> frozenset[str]:
        """Columns present on ``table``, for operational checks and migration tests.

        Identifier interpolation is unavoidable here: SQLite does not bind table names in a
        PRAGMA. The argument is quoted so a caller cannot append a second statement, and the
        only callers are this package's own tests and diagnostics.
        """
        quoted = table.replace('"', '""')
        rows = self._connection.execute(f'PRAGMA table_info("{quoted}")').fetchall()
        return frozenset(str(row[1]) for row in rows)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Repository:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _apply_schema(self) -> None:
        """Bring the connected database up to ``SCHEMA_VERSION``.

        Three properties this has to hold, each of which was a defect first.

        **The version and the shape change together.** A migration step and the version stamp
        are one transaction. SQLite makes both DDL and ``user_version`` transactional, so a
        crash leaves either the old shape with the old version or the new shape with the new
        version. Without that, a crash in the window left a migrated store still claiming the
        old version, and every subsequent open re-ran the ALTER and failed on "duplicate
        column name". The store could not be opened again.

        **A brand-new store is stamped before it is created, not after.** Stamping afterwards
        meant a build that died between the script and the stamp left a current-shaped store
        reporting version 0, which an older build would read as a legacy store and "migrate" —
        the exact case the newer-store refusal exists to prevent. Stamping first makes the
        recorded version an upper bound on the shape, and a partially created store is
        completed by the script on the next open because every statement in it is
        ``IF NOT EXISTS``.

        **Migrations run before the script.** A statement in the script can depend on a column
        a migration adds: the partial unique index on ``decisions.invoice_fingerprint`` cannot
        be created until the column exists.
        """
        with self._write_lock:
            version = self._store_version()
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"{self._db_path} was written by a newer schema (version {version}); "
                    f"this build expects version {SCHEMA_VERSION}. Refusing to open it: "
                    "running older code against a newer store can drop a constraint from the "
                    "enforcement path."
                )
            if version == _UNVERSIONED:
                # No tables at all. Claim the version first, then build the shape.
                self._stamp(SCHEMA_VERSION)
            else:
                for target in range(version + 1, SCHEMA_VERSION + 1):
                    self._migrate_to(target)
            self._connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    def _migrate_to(self, target: int) -> None:
        """Apply one version's steps and its stamp as a single transaction.

        ``BEGIN IMMEDIATE`` takes the write lock at the start rather than on first write, so
        two processes opening an unmigrated store cannot both decide to migrate it. The second
        waits, then finds the steps applied and the version advanced.
        """
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            for step in _MIGRATIONS.get(target, ()):
                if self._step_is_needed(step):
                    self._connection.execute(step.statement)
            self._stamp(target)
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        else:
            self._connection.execute("COMMIT")

    def _step_is_needed(self, step: _AddColumn) -> bool:
        """Whether ``step`` still has work to do against the open store.

        A table that does not exist yet needs nothing: the schema script will create it with
        the column already present.
        """
        columns = self.column_names(step.table)
        return bool(columns) and step.column not in columns

    def _stamp(self, version: int) -> None:
        # Interpolated because PRAGMA takes no bound parameters. The argument comes from this
        # module's own constants, never from a caller, and is formatted as an integer.
        self._connection.execute(f"PRAGMA user_version = {version:d}")

    def _store_version(self) -> int:
        """The schema version of the database on disk.

        ``_UNVERSIONED`` means a database with no tables: a fresh file, which is stamped and
        then created. A store that has tables and no stamp was written before versioning
        existed, and is version 1 — the shape the code had when the pragma was introduced.
        That reading cannot become ambiguous, because a stamp is now written before any shape:
        every store a versioned build creates carries its version from the first statement on.
        """
        pragma = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
        if pragma:
            return pragma
        has_tables = self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchone()
        return 1 if has_tables else _UNVERSIONED

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Cursor]:
        """Take the write lock immediately and hold it for the block.

        ``BEGIN IMMEDIATE`` rather than the default deferred begin: with a deferred
        transaction the lock is acquired at the first write, leaving a window in which two
        callers have both read and neither has written. That window is exactly where a
        duplicate decision would be created.
        """
        with self._write_lock:
            cursor = self._connection.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            try:
                yield cursor
            except Exception:
                cursor.execute("ROLLBACK")
                raise
            else:
                cursor.execute("COMMIT")
            finally:
                cursor.close()

    # ---- runs -------------------------------------------------------------------------

    def create_run(self, state: RunState) -> RunState:
        """Insert a new run at version 1."""
        stored = state.model_copy(update={"version": 1})
        with self._write_transaction() as cursor:
            cursor.execute(
                """
                INSERT INTO runs (run_id, case_id, status, phase, version, state_json,
                                  created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stored.run_id,
                    stored.case_id,
                    stored.status.value,
                    stored.phase.value,
                    stored.version,
                    stored.model_dump_json(),
                    _iso(stored.created_at),
                    _iso(stored.updated_at),
                ),
            )
        return stored

    def load_run(self, run_id: str) -> RunState | None:
        row = self._connection.execute(
            "SELECT state_json FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        return RunState.model_validate_json(row["state_json"])

    def require_run(self, run_id: str) -> RunState:
        state = self.load_run(run_id)
        if state is None:
            raise RunNotFound(run_id)
        return state

    def save_run(self, state: RunState) -> RunState:
        """Persist a run, asserting the version it was read at.

        The caller passes the state it loaded and mutated; this method writes it at
        ``version + 1`` only if the stored row is still at ``version``.
        """
        expected = state.version
        state.touch()
        updated = state.model_copy(update={"version": expected + 1})
        with self._write_transaction() as cursor:
            cursor.execute(
                """
                UPDATE runs
                   SET status = ?, phase = ?, version = ?, state_json = ?, updated_at = ?
                 WHERE run_id = ? AND version = ?
                """,
                (
                    updated.status.value,
                    updated.phase.value,
                    updated.version,
                    updated.model_dump_json(),
                    _iso(updated.updated_at),
                    updated.run_id,
                    expected,
                ),
            )
            if cursor.rowcount == 0:
                actual_row = cursor.execute(
                    "SELECT version FROM runs WHERE run_id = ?", (updated.run_id,)
                ).fetchone()
                if actual_row is None:
                    raise RunNotFound(updated.run_id)
                raise StaleRunVersion(updated.run_id, expected, actual_row["version"])
        return updated

    def list_runs(self, *, limit: int = 50, status: RunStatus | None = None) -> list[RunState]:
        if status is None:
            rows = self._connection.execute(
                "SELECT state_json FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT state_json FROM runs WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status.value, limit),
            ).fetchall()
        return [RunState.model_validate_json(row["state_json"]) for row in rows]

    # ---- events -----------------------------------------------------------------------

    def append_event(
        self,
        *,
        run_id: str,
        event_type: str,
        correlation_id: str,
        payload: dict[str, Any],
        phase: str | None = None,
        outcome: str | None = None,
        duration_ms: int | None = None,
        created_at: datetime | None = None,
    ) -> int:
        """Append one audit event and return its per-run sequence number.

        The sequence is allocated inside the write transaction, so concurrent appends cannot
        produce a gap or a duplicate. The payload must already be redacted; this method does
        not redact, because a single egress point that does is easier to verify than a
        second one here that might diverge.
        """
        moment = created_at or datetime.now(tz=UTC)
        with self._write_transaction() as cursor:
            row = cursor.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS last FROM events WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            sequence = int(row["last"]) + 1
            cursor.execute(
                """
                INSERT INTO events (run_id, sequence, event_type, phase, outcome,
                                    duration_ms, correlation_id, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    sequence,
                    event_type,
                    phase,
                    outcome,
                    duration_ms,
                    correlation_id,
                    json.dumps(payload, default=str),
                    _iso(moment),
                ),
            )
        return sequence

    def list_events(self, run_id: str) -> list[StoredEvent]:
        rows = self._connection.execute(
            "SELECT * FROM events WHERE run_id = ? ORDER BY sequence", (run_id,)
        ).fetchall()
        return [
            StoredEvent(
                event_id=row["event_id"],
                run_id=row["run_id"],
                sequence=row["sequence"],
                event_type=row["event_type"],
                phase=row["phase"],
                outcome=row["outcome"],
                duration_ms=row["duration_ms"],
                correlation_id=row["correlation_id"],
                payload=json.loads(row["payload_json"]),
                created_at=_parse_iso(row["created_at"]),
            )
            for row in rows
        ]

    def count_events(self, run_id: str, event_type: str) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) AS total FROM events WHERE run_id = ? AND event_type = ?",
            (run_id, event_type),
        ).fetchone()
        return int(row["total"])

    # ---- approvals --------------------------------------------------------------------

    def create_approval(self, request: ApprovalRequest) -> ApprovalRequest:
        with self._write_transaction() as cursor:
            cursor.execute(
                """
                INSERT INTO approvals (approval_id, run_id, case_id, status, request_json,
                                       created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    request.approval_id,
                    request.run_id,
                    request.case_id,
                    request.status.value,
                    request.model_dump_json(),
                    _iso(request.created_at),
                ),
            )
        return request

    def load_approval(self, approval_id: str) -> ApprovalRequest | None:
        row = self._connection.execute(
            "SELECT request_json FROM approvals WHERE approval_id = ?", (approval_id,)
        ).fetchone()
        if row is None:
            return None
        approval = ApprovalRequest.model_validate_json(row["request_json"])
        # Signatures live in their own table so the uniqueness constraint can enforce one per
        # person. The stored JSON carries a copy, but the table is authoritative, so it is
        # re-read here rather than trusted.
        cursor = self._connection.cursor()
        try:
            return approval.model_copy(
                update={"signatures": self._load_signatures(cursor, approval_id)}
            )
        finally:
            cursor.close()

    def find_pending_approval(self, run_id: str) -> ApprovalRequest | None:
        row = self._connection.execute(
            """
            SELECT request_json FROM approvals
             WHERE run_id = ? AND status = ?
             ORDER BY created_at DESC LIMIT 1
            """,
            (run_id, ApprovalStatus.PENDING.value),
        ).fetchone()
        if row is None:
            return None
        return ApprovalRequest.model_validate_json(row["request_json"])

    def _load_signatures(self, cursor: sqlite3.Cursor, approval_id: str) -> list[ApprovalSignature]:
        rows = cursor.execute(
            "SELECT * FROM approval_signatures WHERE approval_id = ? ORDER BY signed_at",
            (approval_id,),
        ).fetchall()
        return [
            ApprovalSignature(
                approver_id=row["approver_id"],
                approver_role=row["approver_role"],
                effective_role=row["effective_role"],
                applicable_limit=(
                    Decimal(row["applicable_limit"])
                    if row["applicable_limit"] is not None
                    else None
                ),
                authority_register_version=row["authority_register_version"],
                delegation_applied=row["delegation_applied"],
                is_financial_control=bool(row["is_financial_control"]),
                signed_at=_parse_iso(row["signed_at"]),
                comment=row["comment"],
            )
            for row in rows
        ]

    def add_approval_signature(
        self,
        approval_id: str,
        signature: ApprovalSignature,
    ) -> tuple[ApprovalRequest, bool]:
        """Record one signature towards an approval. Returns the approval and whether this
        signature was a replay.

        The gate is not opened here. This method collects signatures and updates the
        approval's status to APPROVED only once every requirement is met: enough signatures,
        from distinct people, including one from Financial Control when the transaction is
        higher risk under FIN-POL-003 §3.

        Replay detection is the ``(approval_id, approver_id)`` primary key. The same person
        delivering twice collides, and the collision is the detection, so it survives a crash
        between a read and a write exactly as the decision key does.
        """
        with self._write_transaction() as cursor:
            row = cursor.execute(
                "SELECT request_json, status FROM approvals WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            if row is None:
                raise ApprovalStateConflict(f"approval {approval_id} does not exist")

            existing = ApprovalRequest.model_validate_json(row["request_json"])
            stored_status = ApprovalStatus(row["status"])

            if stored_status is ApprovalStatus.REJECTED:
                raise ApprovalStateConflict(
                    f"approval {approval_id} is already REJECTED and cannot be approved"
                )

            cursor.execute(
                """
                INSERT OR IGNORE INTO approval_signatures (
                    approval_id, approver_id, approver_role, effective_role,
                    applicable_limit, authority_register_version, delegation_applied,
                    is_financial_control, signed_at, comment
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval_id,
                    signature.approver_id,
                    signature.approver_role,
                    signature.effective_role,
                    str(signature.applicable_limit)
                    if signature.applicable_limit is not None
                    else None,
                    signature.authority_register_version,
                    signature.delegation_applied,
                    1 if signature.is_financial_control else 0,
                    _iso(signature.signed_at),
                    signature.comment,
                ),
            )
            replayed = cursor.rowcount == 0

            signatures = self._load_signatures(cursor, approval_id)
            updated = existing.model_copy(update={"signatures": signatures})

            if updated.signature_requirement_met:
                last = signatures[-1]
                updated = updated.model_copy(
                    update={
                        "status": ApprovalStatus.APPROVED,
                        "decided_at": last.signed_at,
                        "decided_by": last.approver_id,
                        "decided_by_role": last.approver_role,
                        "decision_comment": last.comment,
                    }
                )

            cursor.execute(
                """
                UPDATE approvals
                   SET status = ?, request_json = ?, decided_at = ?, decided_by = ?,
                       decided_by_role = ?, decision_comment = ?
                 WHERE approval_id = ?
                """,
                (
                    updated.status.value,
                    updated.model_dump_json(),
                    _iso(updated.decided_at) if updated.decided_at else None,
                    updated.decided_by,
                    updated.decided_by_role,
                    updated.decision_comment,
                    approval_id,
                ),
            )
        return updated, replayed

    def reject_approval(
        self,
        approval_id: str,
        *,
        decided_by: str,
        decided_by_role: str,
        comment: str = "",
        decided_at: datetime | None = None,
    ) -> tuple[ApprovalRequest, bool]:
        """Reject an approval outright. Returns the approval and whether this was a replay.

        Any single signatory may reject: a requirement for two approvals is a requirement for
        two people to *agree*, so one refusal settles it. A second identical rejection is a
        replay; a rejection after the approval completed is a conflict, because reversing a
        recorded decision is not something arrival order should decide.
        """
        moment = decided_at or datetime.now(tz=UTC)
        with self._write_transaction() as cursor:
            row = cursor.execute(
                "SELECT request_json, status, decided_by FROM approvals WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            if row is None:
                raise ApprovalStateConflict(f"approval {approval_id} does not exist")

            existing = ApprovalRequest.model_validate_json(row["request_json"])
            stored_status = ApprovalStatus(row["status"])

            if stored_status is ApprovalStatus.REJECTED:
                return existing, True
            if stored_status is ApprovalStatus.APPROVED:
                raise ApprovalStateConflict(
                    f"approval {approval_id} is already APPROVED and cannot be changed to REJECTED"
                )

            rejected = existing.model_copy(
                update={
                    "status": ApprovalStatus.REJECTED,
                    "decided_at": moment,
                    "decided_by": decided_by,
                    "decided_by_role": decided_by_role,
                    "decision_comment": comment,
                }
            )
            cursor.execute(
                """
                UPDATE approvals
                   SET status = ?, request_json = ?, decided_at = ?, decided_by = ?,
                       decided_by_role = ?, decision_comment = ?
                 WHERE approval_id = ? AND status = ?
                """,
                (
                    ApprovalStatus.REJECTED.value,
                    rejected.model_dump_json(),
                    _iso(moment),
                    decided_by,
                    decided_by_role,
                    comment,
                    approval_id,
                    ApprovalStatus.PENDING.value,
                ),
            )
            if cursor.rowcount == 0:  # pragma: no cover - BEGIN IMMEDIATE prevents this
                raise ApprovalStateConflict(f"approval {approval_id} was resolved concurrently")
        return rejected, False

    # ---- decisions --------------------------------------------------------------------

    def record_decision(
        self,
        *,
        idempotency_key: str,
        request_hash: str,
        approval_id: str,
        invoice_fingerprint: str,
        receipt: DecisionReceipt,
    ) -> tuple[DecisionReceipt, bool]:
        """Record an effective finance decision exactly once.

        Returns the receipt and whether this call was a replay. On a replay the *stored*
        receipt is returned, not the one passed in, so the response is byte-identical across
        deliveries and a caller cannot learn anything from retrying.

        Raises ``ApprovalStateConflict`` in two cases, both of which are caller errors rather
        than replays: the same key with different arguments, and a second decision on a run
        that already has one.
        """
        with self._write_transaction() as cursor:
            existing = cursor.execute(
                "SELECT request_hash, response_json FROM decisions WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise ApprovalStateConflict(
                        "idempotency key reused with different decision arguments; refusing "
                        "to return a response for a different request"
                    )
                stored = DecisionReceipt.model_validate_json(existing["response_json"])
                return stored.model_copy(update={"replayed": True}), True

            conflicting = cursor.execute(
                "SELECT idempotency_key FROM decisions WHERE run_id = ?", (receipt.run_id,)
            ).fetchone()
            if conflicting is not None:
                raise ApprovalStateConflict(
                    f"run {receipt.run_id} already has an effective decision; a run records "
                    "at most one"
                )

            # One decision per invoice, across runs. Checked here and enforced by a unique
            # index, so a concurrent second run cannot slip between this read and the insert.
            duplicate = cursor.execute(
                """
                SELECT run_id, case_id FROM decisions
                 WHERE invoice_fingerprint = ? AND invoice_fingerprint <> ''
                """,
                (invoice_fingerprint,),
            ).fetchone()
            if duplicate is not None:
                raise ApprovalStateConflict(
                    f"this invoice was already posted by run {duplicate['run_id']} "
                    f"(case {duplicate['case_id']}); a second posting of the same vendor, "
                    "invoice number, currency and amount is the double-payment path "
                    "FIN-POL-005 exists to prevent"
                )

            cursor.execute(
                """
                INSERT INTO decisions (idempotency_key, run_id, case_id, approval_id, outcome,
                                       request_hash, response_json, created_at,
                                       invoice_fingerprint)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    idempotency_key,
                    receipt.run_id,
                    receipt.case_id,
                    approval_id,
                    receipt.outcome.value,
                    request_hash,
                    receipt.model_dump_json(),
                    _iso(receipt.recorded_at),
                    invoice_fingerprint,
                ),
            )
        return receipt, False

    def load_decision(self, run_id: str) -> DecisionReceipt | None:
        row = self._connection.execute(
            "SELECT response_json FROM decisions WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        return DecisionReceipt.model_validate_json(row["response_json"])

    def count_decisions(self, run_id: str) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) AS total FROM decisions WHERE run_id = ?", (run_id,)
        ).fetchone()
        return int(row["total"])
