"""Repository contract tests.

The idempotency and concurrency tests are the ones that matter. They are the evidence that
"exactly one decision per approved run" is a property of the store rather than of the
orchestrator's control flow, which is the only way it survives a crash between a check and a
write.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from ap_agent.domain.enums import ApprovalStatus, Outcome, RunPhase, RunStatus
from ap_agent.domain.errors import ApprovalStateConflict, RunNotFound
from ap_agent.domain.request import ProcessingRequest
from ap_agent.domain.results import ApprovalRequest, DecisionReceipt
from ap_agent.domain.run_state import RunState
from ap_agent.persistence.repository import (
    Repository,
    StaleRunVersion,
    compute_idempotency_key,
    compute_request_hash,
)

NOW = datetime(2026, 9, 11, 9, 30, tzinfo=UTC)


@pytest.fixture
def repository(tmp_path: Path) -> Repository:
    return Repository(tmp_path / "runtime" / "test.db")


def _request(case_id: str = "FIN-TEST") -> ProcessingRequest:
    return ProcessingRequest(
        case_id=case_id,
        invoice_reference="INV-TEST-1",
        vendor="Test Vendor Pty Ltd",
        vendor_id="V-TEST",
        amount=Decimal("1000.00"),
        currency="AUD",
    )


def _state(case_id: str = "FIN-TEST") -> RunState:
    return RunState.create(_request(case_id), now=NOW)


def _approval(run_id: str, case_id: str = "FIN-TEST") -> ApprovalRequest:
    return ApprovalRequest(
        approval_id=f"apr_{run_id[-8:]}",
        run_id=run_id,
        case_id=case_id,
        requested_outcome=Outcome.APPROVE_FOR_POSTING,
        presented_amount=Decimal("1000.00"),
        presented_currency="AUD",
        presented_vendor="Test Vendor Pty Ltd",
        created_at=NOW,
    )


def _receipt(run_id: str, key: str, *, case_id: str = "FIN-TEST") -> DecisionReceipt:
    return DecisionReceipt(
        decision_ref=f"DEC-{key[:12].upper()}",
        run_id=run_id,
        case_id=case_id,
        outcome=Outcome.APPROVE_FOR_POSTING,
        amount=Decimal("1000.00"),
        currency="AUD",
        idempotency_key=key,
        recorded_at=NOW,
    )


# ---- runs ------------------------------------------------------------------------------


class TestRuns:
    def test_a_created_run_round_trips(self, repository: Repository) -> None:
        created = repository.create_run(_state())
        loaded = repository.load_run(created.run_id)
        assert loaded is not None
        assert loaded.run_id == created.run_id
        assert loaded.request.case_id == "FIN-TEST"
        assert loaded.version == 1

    def test_an_unknown_run_is_not_found(self, repository: Repository) -> None:
        assert repository.load_run("run_missing") is None
        with pytest.raises(RunNotFound):
            repository.require_run("run_missing")

    def test_saving_increments_the_version(self, repository: Repository) -> None:
        state = repository.create_run(_state())
        state.phase = RunPhase.RECONCILE
        saved = repository.save_run(state)
        assert saved.version == 2
        assert repository.require_run(state.run_id).phase is RunPhase.RECONCILE

    def test_a_stale_write_is_rejected(self, repository: Repository) -> None:
        """Optimistic concurrency: two workers cannot both advance one run."""
        state = repository.create_run(_state())
        first = state.model_copy(deep=True)
        second = state.model_copy(deep=True)

        first.phase = RunPhase.RECONCILE
        repository.save_run(first)

        second.phase = RunPhase.ASSESS_RISK
        with pytest.raises(StaleRunVersion, match="reload the run and retry"):
            repository.save_run(second)

        # The first writer's change stands; the second is not silently lost.
        assert repository.require_run(state.run_id).phase is RunPhase.RECONCILE

    def test_state_survives_a_new_connection(self, tmp_path: Path) -> None:
        """The restart case: a fresh process reads what the previous one wrote."""
        db_path = tmp_path / "restart.db"
        first = Repository(db_path)
        state = first.create_run(_state())
        state.steps_used = 4
        state.phase = RunPhase.AWAITING_APPROVAL
        state.status = RunStatus.AWAITING_APPROVAL
        first.save_run(state)
        first.close()

        second = Repository(db_path)
        reloaded = second.require_run(state.run_id)
        assert reloaded.steps_used == 4
        assert reloaded.phase is RunPhase.AWAITING_APPROVAL
        second.close()

    def test_runs_can_be_listed_by_status(self, repository: Repository) -> None:
        held = repository.create_run(_state("FIN-A"))
        held.status = RunStatus.HELD
        repository.save_run(held)
        repository.create_run(_state("FIN-B"))
        assert [state.case_id for state in repository.list_runs(status=RunStatus.HELD)] == ["FIN-A"]


# ---- events ----------------------------------------------------------------------------


class TestEvents:
    def test_events_are_sequenced_per_run(self, repository: Repository) -> None:
        state = repository.create_run(_state())
        for index in range(3):
            repository.append_event(
                run_id=state.run_id,
                event_type="PHASE_STARTED",
                correlation_id=state.correlation_id,
                payload={"index": index},
            )
        events = repository.list_events(state.run_id)
        assert [event.sequence for event in events] == [1, 2, 3]

    def test_sequences_are_independent_between_runs(self, repository: Repository) -> None:
        first = repository.create_run(_state("FIN-A"))
        second = repository.create_run(_state("FIN-B"))
        repository.append_event(run_id=first.run_id, event_type="X", correlation_id="c", payload={})
        repository.append_event(
            run_id=second.run_id, event_type="X", correlation_id="c", payload={}
        )
        assert repository.list_events(first.run_id)[0].sequence == 1
        assert repository.list_events(second.run_id)[0].sequence == 1

    def test_duration_and_outcome_are_stored(self, repository: Repository) -> None:
        state = repository.create_run(_state())
        repository.append_event(
            run_id=state.run_id,
            event_type="TOOL_CALL",
            correlation_id=state.correlation_id,
            payload={"tool": "get_vendor_record"},
            outcome="SUCCESS",
            duration_ms=42,
        )
        event = repository.list_events(state.run_id)[0]
        assert event.outcome == "SUCCESS"
        assert event.duration_ms == 42
        assert event.payload["tool"] == "get_vendor_record"

    def test_events_are_counted_by_type(self, repository: Repository) -> None:
        state = repository.create_run(_state())
        for _ in range(2):
            repository.append_event(
                run_id=state.run_id, event_type="TOOL_CALL", correlation_id="c", payload={}
            )
        assert repository.count_events(state.run_id, "TOOL_CALL") == 2
        assert repository.count_events(state.run_id, "MODEL_CALL") == 0

    def test_concurrent_appends_do_not_collide(self, repository: Repository) -> None:
        """The sequence is allocated inside the write transaction, so there are no gaps."""
        state = repository.create_run(_state())
        errors: list[Exception] = []

        def append(index: int) -> None:
            try:
                repository.append_event(
                    run_id=state.run_id,
                    event_type="TOOL_CALL",
                    correlation_id="c",
                    payload={"index": index},
                )
            except Exception as error:  # collected, then asserted on after the join
                errors.append(error)

        threads = [threading.Thread(target=append, args=(index,)) for index in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []
        sequences = [event.sequence for event in repository.list_events(state.run_id)]
        assert sequences == list(range(1, 13))


# ---- approvals -------------------------------------------------------------------------


class TestApprovals:
    def test_an_approval_starts_pending_and_is_findable(self, repository: Repository) -> None:
        state = repository.create_run(_state())
        approval = repository.create_approval(_approval(state.run_id))
        pending = repository.find_pending_approval(state.run_id)
        assert pending is not None
        assert pending.approval_id == approval.approval_id
        assert pending.status is ApprovalStatus.PENDING

    def test_resolving_records_the_decider(self, repository: Repository) -> None:
        state = repository.create_run(_state())
        approval = repository.create_approval(_approval(state.run_id))
        resolved, replayed = repository.resolve_approval(
            approval.approval_id,
            status=ApprovalStatus.APPROVED,
            decided_by="U-3081",
            decided_by_role="DEPARTMENT_DIRECTOR",
            comment="ok",
            decided_at=NOW,
        )
        assert not replayed
        assert resolved.status is ApprovalStatus.APPROVED
        assert resolved.decided_by == "U-3081"
        assert repository.find_pending_approval(state.run_id) is None

    def test_the_same_decision_delivered_twice_is_a_replay(self, repository: Repository) -> None:
        state = repository.create_run(_state())
        approval = repository.create_approval(_approval(state.run_id))
        first, first_replayed = repository.resolve_approval(
            approval.approval_id,
            status=ApprovalStatus.APPROVED,
            decided_by="U-3081",
            decided_by_role="DEPARTMENT_DIRECTOR",
        )
        second, second_replayed = repository.resolve_approval(
            approval.approval_id,
            status=ApprovalStatus.APPROVED,
            decided_by="U-3081",
            decided_by_role="DEPARTMENT_DIRECTOR",
        )
        assert not first_replayed
        assert second_replayed
        assert second.decided_at == first.decided_at, "a replay must not move the decision time"

    def test_a_contradictory_second_decision_is_a_conflict(self, repository: Repository) -> None:
        """One approver approving and another rejecting is a real disagreement.

        Resolving it by arrival order would hide it, so the second delivery raises.
        """
        state = repository.create_run(_state())
        approval = repository.create_approval(_approval(state.run_id))
        repository.resolve_approval(
            approval.approval_id,
            status=ApprovalStatus.APPROVED,
            decided_by="U-3081",
            decided_by_role="DEPARTMENT_DIRECTOR",
        )
        with pytest.raises(ApprovalStateConflict, match="already APPROVED"):
            repository.resolve_approval(
                approval.approval_id,
                status=ApprovalStatus.REJECTED,
                decided_by="U-9000",
                decided_by_role="CFO",
            )

    def test_resolving_an_unknown_approval_raises(self, repository: Repository) -> None:
        with pytest.raises(ApprovalStateConflict, match="does not exist"):
            repository.resolve_approval(
                "apr_missing",
                status=ApprovalStatus.APPROVED,
                decided_by="U-1",
                decided_by_role="CFO",
            )

    def test_resolving_to_pending_is_refused(self, repository: Repository) -> None:
        state = repository.create_run(_state())
        approval = repository.create_approval(_approval(state.run_id))
        with pytest.raises(ValueError, match="APPROVED or REJECTED"):
            repository.resolve_approval(
                approval.approval_id,
                status=ApprovalStatus.PENDING,
                decided_by="U-1",
                decided_by_role="CFO",
            )


# ---- decisions -------------------------------------------------------------------------


class TestDecisionIdempotency:
    """The property the whole design turns on: one effective decision per approved run."""

    def test_the_first_call_records_the_decision(self, repository: Repository) -> None:
        state = repository.create_run(_state())
        key = compute_idempotency_key(
            run_id=state.run_id, approval_id="apr_1", outcome=Outcome.APPROVE_FOR_POSTING
        )
        receipt, replayed = repository.record_decision(
            idempotency_key=key,
            request_hash=compute_request_hash({"amount": "1000.00"}),
            approval_id="apr_1",
            receipt=_receipt(state.run_id, key),
        )
        assert not replayed
        assert repository.count_decisions(state.run_id) == 1
        assert repository.load_decision(state.run_id) == receipt

    def test_the_same_key_returns_the_stored_response(self, repository: Repository) -> None:
        state = repository.create_run(_state())
        key = compute_idempotency_key(
            run_id=state.run_id, approval_id="apr_1", outcome=Outcome.APPROVE_FOR_POSTING
        )
        payload = compute_request_hash({"amount": "1000.00"})
        first, _ = repository.record_decision(
            idempotency_key=key,
            request_hash=payload,
            approval_id="apr_1",
            receipt=_receipt(state.run_id, key),
        )
        # A later receipt with a different reference must not overwrite the stored one.
        later = _receipt(state.run_id, key).model_copy(
            update={"decision_ref": "DEC-DIFFERENT", "recorded_at": NOW + timedelta(hours=1)}
        )
        second, replayed = repository.record_decision(
            idempotency_key=key,
            request_hash=payload,
            approval_id="apr_1",
            receipt=later,
        )
        assert replayed
        assert second.decision_ref == first.decision_ref
        assert second.recorded_at == first.recorded_at
        assert second.replayed is True
        assert repository.count_decisions(state.run_id) == 1

    def test_the_same_key_with_different_arguments_is_a_conflict(
        self, repository: Repository
    ) -> None:
        """Returning the stored response would answer a different question from the one asked."""
        state = repository.create_run(_state())
        key = compute_idempotency_key(
            run_id=state.run_id, approval_id="apr_1", outcome=Outcome.APPROVE_FOR_POSTING
        )
        repository.record_decision(
            idempotency_key=key,
            request_hash=compute_request_hash({"amount": "1000.00"}),
            approval_id="apr_1",
            receipt=_receipt(state.run_id, key),
        )
        with pytest.raises(ApprovalStateConflict, match="different decision arguments"):
            repository.record_decision(
                idempotency_key=key,
                request_hash=compute_request_hash({"amount": "9999.00"}),
                approval_id="apr_1",
                receipt=_receipt(state.run_id, key),
            )

    def test_a_second_decision_on_the_same_run_is_refused(self, repository: Repository) -> None:
        """Even a caller that invented a fresh key cannot post twice against one run."""
        state = repository.create_run(_state())
        first_key = compute_idempotency_key(
            run_id=state.run_id, approval_id="apr_1", outcome=Outcome.APPROVE_FOR_POSTING
        )
        repository.record_decision(
            idempotency_key=first_key,
            request_hash="h1",
            approval_id="apr_1",
            receipt=_receipt(state.run_id, first_key),
        )
        second_key = compute_idempotency_key(
            run_id=state.run_id, approval_id="apr_2", outcome=Outcome.APPROVE_FOR_POSTING
        )
        assert second_key != first_key
        with pytest.raises(ApprovalStateConflict, match="already has an effective decision"):
            repository.record_decision(
                idempotency_key=second_key,
                request_hash="h2",
                approval_id="apr_2",
                receipt=_receipt(state.run_id, second_key),
            )
        assert repository.count_decisions(state.run_id) == 1

    def test_concurrent_identical_calls_produce_one_decision(self, tmp_path: Path) -> None:
        """The race the database constraint exists to win.

        Each thread opens its own connection, which is what a second process would do.
        """
        db_path = tmp_path / "concurrent.db"
        setup = Repository(db_path)
        state = setup.create_run(_state())
        setup.close()

        key = compute_idempotency_key(
            run_id=state.run_id, approval_id="apr_1", outcome=Outcome.APPROVE_FOR_POSTING
        )
        results: list[tuple[str, bool]] = []
        errors: list[Exception] = []
        barrier = threading.Barrier(6)

        def attempt() -> None:
            repository = Repository(db_path)
            try:
                barrier.wait(timeout=10)
                receipt, replayed = repository.record_decision(
                    idempotency_key=key,
                    request_hash="same",
                    approval_id="apr_1",
                    receipt=_receipt(state.run_id, key),
                )
                results.append((receipt.decision_ref, replayed))
            except sqlite3.OperationalError as error:  # pragma: no cover - lock contention
                errors.append(error)
            finally:
                repository.close()

        threads = [threading.Thread(target=attempt) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        verify = Repository(db_path)
        try:
            assert verify.count_decisions(state.run_id) == 1, (
                f"expected one decision, errors were {errors}"
            )
        finally:
            verify.close()

        assert errors == [], f"unexpected lock errors: {errors}"
        assert len(results) == 6
        assert len({reference for reference, _ in results}) == 1, (
            "every caller must receive the same decision reference"
        )
        assert sum(1 for _, replayed in results if not replayed) == 1, (
            "exactly one caller may be the effective one"
        )


class TestIdempotencyKeyDerivation:
    def test_the_key_is_stable_for_the_same_run_and_approval(self) -> None:
        first = compute_idempotency_key(
            run_id="run_1", approval_id="apr_1", outcome=Outcome.APPROVE_FOR_POSTING
        )
        second = compute_idempotency_key(
            run_id="run_1", approval_id="apr_1", outcome=Outcome.APPROVE_FOR_POSTING
        )
        assert first == second

    def test_the_key_changes_with_the_outcome(self) -> None:
        approve = compute_idempotency_key(
            run_id="run_1", approval_id="apr_1", outcome=Outcome.APPROVE_FOR_POSTING
        )
        reject = compute_idempotency_key(
            run_id="run_1", approval_id="apr_1", outcome=Outcome.REJECT_DUPLICATE
        )
        assert approve != reject

    def test_the_key_changes_between_runs(self) -> None:
        assert compute_idempotency_key(
            run_id="run_1", approval_id="apr_1", outcome=Outcome.APPROVE_FOR_POSTING
        ) != compute_idempotency_key(
            run_id="run_2", approval_id="apr_1", outcome=Outcome.APPROVE_FOR_POSTING
        )

    def test_the_request_hash_is_order_insensitive(self) -> None:
        """Key ordering in a payload must not change the hash, or a replay would look like a
        conflict."""
        assert compute_request_hash({"a": 1, "b": 2}) == compute_request_hash({"b": 2, "a": 1})

    def test_the_request_hash_changes_with_the_payload(self) -> None:
        assert compute_request_hash({"amount": "1.00"}) != compute_request_hash({"amount": "2.00"})
