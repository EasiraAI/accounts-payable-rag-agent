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
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict

from ap_agent.domain.enums import ApprovalStatus, Outcome, RunStatus
from ap_agent.domain.errors import ApprovalStateConflict, RunNotFound
from ap_agent.domain.results import ApprovalRequest, DecisionReceipt
from ap_agent.domain.run_state import RunState

_SCHEMA_PATH: Final = Path(__file__).with_name("schema.sql")

#: SQLite busy timeout. Long enough to ride out a concurrent writer on a local file,
#: short enough that a genuine deadlock surfaces rather than hanging a request.
_BUSY_TIMEOUT_SECONDS: Final = 5.0


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


def compute_idempotency_key(*, run_id: str, approval_id: str, outcome: Outcome) -> str:
    """Derive the idempotency key for a finance decision.

    Computed here from values the system controls, never accepted from a caller. A
    caller-supplied key would let a client defeat the guarantee by varying it, which is the
    opposite of what an idempotency key is for. Keying on the run and the approval means a
    replayed callback for the same approval produces the same key, while a genuinely
    different approval on the same run produces a different one and is then refused by the
    ``run_id`` uniqueness constraint.
    """
    material = f"{run_id}|{approval_id}|{outcome.value}"
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
        self._apply_schema()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Repository:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _apply_schema(self) -> None:
        self._connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Cursor]:
        """Take the write lock immediately and hold it for the block.

        ``BEGIN IMMEDIATE`` rather than the default deferred begin: with a deferred
        transaction the lock is acquired at the first write, leaving a window in which two
        callers have both read and neither has written. That window is exactly where a
        duplicate decision would be created.
        """
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
        return ApprovalRequest.model_validate_json(row["request_json"])

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

    def resolve_approval(
        self,
        approval_id: str,
        *,
        status: ApprovalStatus,
        decided_by: str,
        decided_by_role: str,
        comment: str = "",
        decided_at: datetime | None = None,
    ) -> tuple[ApprovalRequest, bool]:
        """Record a human decision on an approval. Returns the approval and whether this was
        a replay.

        A second delivery of the same decision is a replay and returns the stored approval
        unchanged. A second delivery of a *different* decision is a conflict and raises: one
        approver approving and another rejecting the same request is a real disagreement
        that a system must surface rather than resolve by arrival order.
        """
        if status is ApprovalStatus.PENDING:
            raise ValueError("resolve_approval requires APPROVED or REJECTED")
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

            if stored_status is not ApprovalStatus.PENDING:
                if stored_status is status:
                    return existing, True
                raise ApprovalStateConflict(
                    f"approval {approval_id} is already {stored_status.value} and cannot be "
                    f"changed to {status.value}"
                )

            resolved = existing.model_copy(
                update={
                    "status": status,
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
                    status.value,
                    resolved.model_dump_json(),
                    _iso(moment),
                    decided_by,
                    decided_by_role,
                    comment,
                    approval_id,
                    ApprovalStatus.PENDING.value,
                ),
            )
            if cursor.rowcount == 0:
                # Another caller resolved it between the read and the update inside this
                # transaction. Cannot happen under BEGIN IMMEDIATE, but asserting it means a
                # future change to the locking strategy fails loudly rather than silently.
                raise ApprovalStateConflict(f"approval {approval_id} was resolved concurrently")
        return resolved, False

    # ---- decisions --------------------------------------------------------------------

    def record_decision(
        self,
        *,
        idempotency_key: str,
        request_hash: str,
        approval_id: str,
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

            cursor.execute(
                """
                INSERT INTO decisions (idempotency_key, run_id, case_id, approval_id, outcome,
                                       request_hash, response_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
