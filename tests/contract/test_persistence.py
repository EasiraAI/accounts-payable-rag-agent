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
from ap_agent.domain.results import ApprovalRequest, ApprovalSignature, DecisionReceipt
from ap_agent.domain.run_state import RunState
from ap_agent.persistence.repository import (
    SCHEMA_VERSION,
    Repository,
    StaleRunVersion,
    compute_idempotency_key,
    compute_invoice_fingerprint,
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


def _key(run_id: str, approval_id: str = "apr_1") -> str:
    """The idempotency key now depends on the monetary facts as well as the run.

    A security review mutated the amount after approval and posted the inflated figure under
    the original key, so the amount, currency and vendor are part of the material.
    """
    return compute_idempotency_key(
        run_id=run_id,
        approval_id=approval_id,
        outcome=Outcome.APPROVE_FOR_POSTING,
        amount=Decimal("1000.00"),
        currency="AUD",
        vendor_id="V-TEST",
    )


def _fingerprint(reference: str = "INV-TEST-1") -> str:
    return compute_invoice_fingerprint(
        vendor_id="V-TEST",
        invoice_reference=reference,
        currency="AUD",
        gross_amount=Decimal("1000.00"),
    )


def _signature(approver_id: str, *, role: str = "DEPARTMENT_DIRECTOR", fc: bool = False):
    return ApprovalSignature(
        approver_id=approver_id,
        approver_role=role,
        effective_role=role,
        applicable_limit=Decimal("50000"),
        authority_register_version="FIN-POL-003 v4.0",
        is_financial_control=fc,
        signed_at=NOW,
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


class TestApprovalSignatures:
    """An approval is a set of signatures, not a single decision.

    FIN-POL-003 §3 requires two approvals for a higher-risk transaction, one from Financial
    Control. An earlier design stored one decider and a flag nothing read, and two
    independent reviews posted a bank-change case on a single approval.
    """

    def test_an_approval_starts_pending_and_is_findable(self, repository: Repository) -> None:
        state = repository.create_run(_state())
        approval = repository.create_approval(_approval(state.run_id))
        pending = repository.find_pending_approval(state.run_id)
        assert pending is not None
        assert pending.approval_id == approval.approval_id
        assert pending.status is ApprovalStatus.PENDING
        assert pending.signatures_collected == 0

    def test_one_signature_settles_a_single_signature_approval(
        self, repository: Repository
    ) -> None:
        state = repository.create_run(_state())
        approval = repository.create_approval(_approval(state.run_id))
        resolved, replayed = repository.add_approval_signature(
            approval.approval_id, _signature("U-3081")
        )
        assert not replayed
        assert resolved.status is ApprovalStatus.APPROVED
        assert resolved.signature_requirement_met
        assert resolved.decided_by == "U-3081"

    def test_one_signature_does_not_settle_a_two_signature_approval(
        self, repository: Repository
    ) -> None:
        """The branch that was missing. This is the release blocker both reviews found."""
        state = repository.create_run(_state())
        approval = repository.create_approval(
            _approval(state.run_id).model_copy(
                update={"required_signature_count": 2, "requires_financial_control": True}
            )
        )
        resolved, _ = repository.add_approval_signature(approval.approval_id, _signature("U-3081"))
        assert resolved.status is ApprovalStatus.PENDING
        assert not resolved.signature_requirement_met
        assert resolved.signatures_outstanding == 1
        assert "Financial Control" in resolved.outstanding_requirement_detail()

    def test_two_distinct_signatures_with_financial_control_settle_it(
        self, repository: Repository
    ) -> None:
        state = repository.create_run(_state())
        approval = repository.create_approval(
            _approval(state.run_id).model_copy(
                update={"required_signature_count": 2, "requires_financial_control": True}
            )
        )
        repository.add_approval_signature(approval.approval_id, _signature("U-3081"))
        resolved, _ = repository.add_approval_signature(
            approval.approval_id,
            _signature("U-4400", role="FINANCIAL_CONTROL", fc=True),
        )
        assert resolved.status is ApprovalStatus.APPROVED
        assert resolved.signature_requirement_met
        assert resolved.has_financial_control_signature

    def test_two_signatures_without_financial_control_do_not_settle_it(
        self, repository: Repository
    ) -> None:
        """FIN-POL-003 §3: "One approver must be from Financial Control." """
        state = repository.create_run(_state())
        approval = repository.create_approval(
            _approval(state.run_id).model_copy(
                update={"required_signature_count": 2, "requires_financial_control": True}
            )
        )
        repository.add_approval_signature(approval.approval_id, _signature("U-3081"))
        resolved, _ = repository.add_approval_signature(
            approval.approval_id, _signature("U-2001", role="EXECUTIVE_DIRECTOR")
        )
        assert resolved.status is ApprovalStatus.PENDING
        assert not resolved.signature_requirement_met

    def test_the_same_person_cannot_sign_twice_towards_two(self, repository: Repository) -> None:
        """A second signature from the same person satisfies nothing the first did not."""
        state = repository.create_run(_state())
        approval = repository.create_approval(
            _approval(state.run_id).model_copy(
                update={"required_signature_count": 2, "requires_financial_control": False}
            )
        )
        repository.add_approval_signature(approval.approval_id, _signature("U-3081"))
        resolved, replayed = repository.add_approval_signature(
            approval.approval_id, _signature("U-3081")
        )
        assert replayed, "the same approver signing again is a replay"
        assert resolved.signatures_collected == 1
        assert resolved.status is ApprovalStatus.PENDING

    def test_a_duplicate_signature_is_a_replay(self, repository: Repository) -> None:
        state = repository.create_run(_state())
        approval = repository.create_approval(_approval(state.run_id))
        first, first_replayed = repository.add_approval_signature(
            approval.approval_id, _signature("U-3081")
        )
        second, second_replayed = repository.add_approval_signature(
            approval.approval_id, _signature("U-3081")
        )
        assert not first_replayed
        assert second_replayed
        assert second.signatures_collected == 1
        assert second.decided_at == first.decided_at

    def test_rejecting_settles_the_approval(self, repository: Repository) -> None:
        state = repository.create_run(_state())
        approval = repository.create_approval(_approval(state.run_id))
        resolved, replayed = repository.reject_approval(
            approval.approval_id,
            decided_by="U-3081",
            decided_by_role="DEPARTMENT_DIRECTOR",
            comment="not satisfied",
        )
        assert not replayed
        assert resolved.status is ApprovalStatus.REJECTED
        assert repository.find_pending_approval(state.run_id) is None

    def test_a_duplicate_rejection_is_a_replay(self, repository: Repository) -> None:
        state = repository.create_run(_state())
        approval = repository.create_approval(_approval(state.run_id))
        repository.reject_approval(
            approval.approval_id, decided_by="U-3081", decided_by_role="DEPARTMENT_DIRECTOR"
        )
        _, replayed = repository.reject_approval(
            approval.approval_id, decided_by="U-3081", decided_by_role="DEPARTMENT_DIRECTOR"
        )
        assert replayed

    def test_signing_a_rejected_approval_is_a_conflict(self, repository: Repository) -> None:
        state = repository.create_run(_state())
        approval = repository.create_approval(_approval(state.run_id))
        repository.reject_approval(
            approval.approval_id, decided_by="U-3081", decided_by_role="DEPARTMENT_DIRECTOR"
        )
        with pytest.raises(ApprovalStateConflict, match="already REJECTED"):
            repository.add_approval_signature(approval.approval_id, _signature("U-9000"))

    def test_rejecting_a_settled_approval_is_a_conflict(self, repository: Repository) -> None:
        """One approver approving and another rejecting is a real disagreement."""
        state = repository.create_run(_state())
        approval = repository.create_approval(_approval(state.run_id))
        repository.add_approval_signature(approval.approval_id, _signature("U-3081"))
        with pytest.raises(ApprovalStateConflict, match="already APPROVED"):
            repository.reject_approval(
                approval.approval_id, decided_by="U-9000", decided_by_role="CFO"
            )

    def test_signing_an_unknown_approval_raises(self, repository: Repository) -> None:
        with pytest.raises(ApprovalStateConflict, match="does not exist"):
            repository.add_approval_signature("apr_missing", _signature("U-1"))

    def test_signatures_survive_a_reload(self, repository: Repository) -> None:
        """The signature table is authoritative, so a reload re-reads it."""
        state = repository.create_run(_state())
        approval = repository.create_approval(
            _approval(state.run_id).model_copy(update={"required_signature_count": 2})
        )
        repository.add_approval_signature(approval.approval_id, _signature("U-3081"))
        reloaded = repository.load_approval(approval.approval_id)
        assert reloaded is not None
        assert reloaded.signatures_collected == 1
        assert reloaded.signatures[0].applicable_limit == Decimal("50000")
        assert reloaded.signatures[0].authority_register_version == "FIN-POL-003 v4.0"


# ---- decisions -------------------------------------------------------------------------


class TestDecisionIdempotency:
    """The property the whole design turns on: one effective decision per approved run."""

    def test_the_first_call_records_the_decision(self, repository: Repository) -> None:
        state = repository.create_run(_state())
        key = _key(state.run_id)
        receipt, replayed = repository.record_decision(
            idempotency_key=key,
            request_hash=compute_request_hash({"amount": "1000.00"}),
            approval_id="apr_1",
            invoice_fingerprint=_fingerprint(),
            receipt=_receipt(state.run_id, key),
        )
        assert not replayed
        assert repository.count_decisions(state.run_id) == 1
        assert repository.load_decision(state.run_id) == receipt

    def test_the_same_key_returns_the_stored_response(self, repository: Repository) -> None:
        state = repository.create_run(_state())
        key = _key(state.run_id)
        payload = compute_request_hash({"amount": "1000.00"})
        first, _ = repository.record_decision(
            idempotency_key=key,
            request_hash=payload,
            approval_id="apr_1",
            invoice_fingerprint=_fingerprint(),
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
            invoice_fingerprint=_fingerprint(),
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
        key = _key(state.run_id)
        repository.record_decision(
            idempotency_key=key,
            request_hash=compute_request_hash({"amount": "1000.00"}),
            approval_id="apr_1",
            invoice_fingerprint=_fingerprint(),
            receipt=_receipt(state.run_id, key),
        )
        with pytest.raises(ApprovalStateConflict, match="different decision arguments"):
            repository.record_decision(
                idempotency_key=key,
                request_hash=compute_request_hash({"amount": "9999.00"}),
                approval_id="apr_1",
                invoice_fingerprint=_fingerprint(),
                receipt=_receipt(state.run_id, key),
            )

    def test_a_second_decision_on_the_same_run_is_refused(self, repository: Repository) -> None:
        """Even a caller that invented a fresh key cannot post twice against one run."""
        state = repository.create_run(_state())
        first_key = _key(state.run_id, "apr_1")
        repository.record_decision(
            idempotency_key=first_key,
            request_hash="h1",
            approval_id="apr_1",
            invoice_fingerprint=_fingerprint(),
            receipt=_receipt(state.run_id, first_key),
        )
        second_key = _key(state.run_id, "apr_2")
        assert second_key != first_key
        with pytest.raises(ApprovalStateConflict, match="already has an effective decision"):
            repository.record_decision(
                idempotency_key=second_key,
                request_hash="h2",
                approval_id="apr_2",
                invoice_fingerprint=_fingerprint("INV-OTHER"),
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

        key = _key(state.run_id)
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
                    invoice_fingerprint=_fingerprint(),
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
    @staticmethod
    def _derive(**overrides: object) -> str:
        material: dict[str, object] = {
            "run_id": "run_1",
            "approval_id": "apr_1",
            "outcome": Outcome.APPROVE_FOR_POSTING,
            "amount": Decimal("1000.00"),
            "currency": "AUD",
            "vendor_id": "V-TEST",
        }
        material.update(overrides)
        return compute_idempotency_key(**material)  # type: ignore[arg-type]

    def test_the_key_is_stable_for_identical_material(self) -> None:
        assert self._derive() == self._derive()

    def test_the_key_changes_with_the_outcome(self) -> None:
        assert self._derive() != self._derive(outcome=Outcome.REJECT_DUPLICATE)

    def test_the_key_changes_between_runs(self) -> None:
        assert self._derive() != self._derive(run_id="run_2")

    def test_the_key_changes_with_the_amount(self) -> None:
        """The fix for the amount-inflation finding.

        A security review parked a run at the gate, mutated the request amount, and posted
        the inflated figure under the original approval, because the key did not depend on
        the amount. A changed amount now produces a different key, which the run_id
        uniqueness constraint refuses outright.
        """
        assert self._derive() != self._derive(amount=Decimal("999999.00"))

    def test_the_key_changes_with_the_currency(self) -> None:
        assert self._derive() != self._derive(currency="USD")

    def test_the_key_changes_with_the_vendor(self) -> None:
        assert self._derive() != self._derive(vendor_id="V-OTHER")

    def test_the_request_hash_is_order_insensitive(self) -> None:
        """Key ordering in a payload must not change the hash, or a replay would look like a
        conflict."""
        assert compute_request_hash({"a": 1, "b": 2}) == compute_request_hash({"b": 2, "a": 1})

    def test_the_request_hash_changes_with_the_payload(self) -> None:
        assert compute_request_hash({"amount": "1.00"}) != compute_request_hash({"amount": "2.00"})


class TestInvoiceLevelDuplicatePrevention:
    """One decision per invoice, across runs.

    Per-run uniqueness left the double-payment path open. A security review submitted one
    identical invoice as three separate runs, approved each once, and received three posted
    decisions. The duplicate-detection rule could not have caught it: it reads an upstream
    history service that knows nothing about decisions this system recorded seconds earlier.
    """

    def test_the_same_invoice_cannot_post_through_a_second_run(
        self, repository: Repository
    ) -> None:
        first = repository.create_run(_state("FIN-A"))
        second = repository.create_run(_state("FIN-B"))
        fingerprint = _fingerprint()

        first_key = _key(first.run_id)
        repository.record_decision(
            idempotency_key=first_key,
            request_hash="h1",
            approval_id="apr_1",
            invoice_fingerprint=fingerprint,
            receipt=_receipt(first.run_id, first_key, case_id="FIN-A"),
        )

        second_key = _key(second.run_id)
        with pytest.raises(ApprovalStateConflict, match="already posted by run"):
            repository.record_decision(
                idempotency_key=second_key,
                request_hash="h2",
                approval_id="apr_2",
                invoice_fingerprint=fingerprint,
                receipt=_receipt(second.run_id, second_key, case_id="FIN-B"),
            )
        assert repository.count_decisions(second.run_id) == 0

    def test_a_different_invoice_posts_normally(self, repository: Repository) -> None:
        first = repository.create_run(_state("FIN-A"))
        second = repository.create_run(_state("FIN-B"))
        first_key = _key(first.run_id)
        repository.record_decision(
            idempotency_key=first_key,
            request_hash="h1",
            approval_id="apr_1",
            invoice_fingerprint=_fingerprint("INV-ONE"),
            receipt=_receipt(first.run_id, first_key, case_id="FIN-A"),
        )
        second_key = _key(second.run_id)
        repository.record_decision(
            idempotency_key=second_key,
            request_hash="h2",
            approval_id="apr_2",
            invoice_fingerprint=_fingerprint("INV-TWO"),
            receipt=_receipt(second.run_id, second_key, case_id="FIN-B"),
        )
        assert repository.count_decisions(second.run_id) == 1

    def test_the_fingerprint_normalises_the_invoice_number(self) -> None:
        """It must agree with the duplicate rule about what counts as the same invoice."""
        assert compute_invoice_fingerprint(
            vendor_id="V-1",
            invoice_reference="inv/2026-0388",
            currency="aud",
            gross_amount=Decimal("100.00"),
        ) == compute_invoice_fingerprint(
            vendor_id="V-1",
            invoice_reference="INV20260388",
            currency="AUD",
            gross_amount=Decimal("100.00"),
        )

    def test_the_fingerprint_distinguishes_the_amount(self) -> None:
        assert compute_invoice_fingerprint(
            vendor_id="V-1",
            invoice_reference="INV-1",
            currency="AUD",
            gross_amount=Decimal("100.00"),
        ) != compute_invoice_fingerprint(
            vendor_id="V-1",
            invoice_reference="INV-1",
            currency="AUD",
            gross_amount=Decimal("100.01"),
        )


class TestSchemaVersioning:
    """Opening a store written by an earlier build must migrate it, not fail at first query.

    The regression these cover: every CREATE in schema.sql is ``IF NOT EXISTS``, so applying
    the script to an existing database reports success while leaving the old columns in
    place. A store created before ``decisions.invoice_fingerprint`` existed therefore opened
    cleanly and raised "no such column" on the first decision, at run time rather than at
    deploy time.
    """

    @staticmethod
    def _legacy_store(path: Path) -> None:
        """A version-1 database: the shape the code had before the fingerprint column.

        Written with raw SQL rather than an old copy of the repository, because the point of
        the test is the column being absent. Only the tables the migration touches, plus the
        ``runs`` row that marks the store as populated rather than fresh.
        """
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, status TEXT NOT NULL,
                phase TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
                state_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE decisions (
                idempotency_key TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE,
                case_id TEXT NOT NULL, approval_id TEXT NOT NULL, outcome TEXT NOT NULL,
                request_hash TEXT NOT NULL, response_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            INSERT INTO decisions VALUES
                ('key-1', 'run-1', 'FIN-001', 'apr-1', 'APPROVE_FOR_POSTING',
                 'hash-1', '{}', '2026-01-01T00:00:00+00:00');
            INSERT INTO runs VALUES
                ('run-1', 'FIN-001', 'COMPLETED', 'COMPLETE', 1, '{}',
                 '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00');
            """
        )
        connection.commit()
        connection.close()

    def test_a_legacy_store_is_migrated_forward(self, tmp_path: Path) -> None:
        path = tmp_path / "legacy.db"
        self._legacy_store(path)

        with Repository(path) as repository:
            columns = repository.column_names("decisions")
            version = repository.schema_version

        assert "invoice_fingerprint" in columns
        assert version == SCHEMA_VERSION

    def test_the_migration_preserves_existing_rows(self, tmp_path: Path) -> None:
        """A migration that loses a recorded decision would re-open the double-payment path."""
        path = tmp_path / "legacy.db"
        self._legacy_store(path)

        with Repository(path):
            pass  # opening the store is what migrates it

        connection = sqlite3.connect(path)
        rows = connection.execute(
            "SELECT idempotency_key, invoice_fingerprint FROM decisions"
        ).fetchall()
        connection.close()

        assert rows == [("key-1", "")]

    def test_migrated_rows_do_not_collide_on_the_partial_index(self, tmp_path: Path) -> None:
        """The empty default must not make two historical decisions look like one invoice.

        This is why the unique index is partial. A plain unique index would have made the
        second pre-existing row unwritable, turning the migration into data loss.
        """
        path = tmp_path / "legacy.db"
        self._legacy_store(path)
        connection = sqlite3.connect(path)
        connection.execute(
            "INSERT INTO decisions VALUES ('key-2', 'run-2', 'FIN-002', 'apr-2',"
            " 'REJECT_DUPLICATE', 'hash-2', '{}', '2026-01-02T00:00:00+00:00')"
        )
        connection.commit()
        connection.close()

        with Repository(path):
            pass

        connection = sqlite3.connect(path)
        count = connection.execute("SELECT count(*) FROM decisions").fetchone()[0]
        connection.close()

        assert count == 2

    def test_a_store_from_a_newer_build_is_refused(self, tmp_path: Path) -> None:
        """Refuse rather than proceed: a rollback must not silently drop a constraint."""
        path = tmp_path / "future.db"
        with Repository(path):
            pass
        connection = sqlite3.connect(path)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1:d}")
        connection.close()

        with pytest.raises(RuntimeError, match="newer schema"):
            Repository(path)

    def test_a_fresh_store_needs_no_migration(self, tmp_path: Path) -> None:
        with Repository(tmp_path / "fresh.db") as repository:
            version = repository.schema_version
        assert version == SCHEMA_VERSION


class TestMigrationCrashWindows:
    """A crash during a migration must not leave a store that cannot be opened again.

    Each of these reproduces a window a review found by inspection and then confirmed by
    simulating the crash. The first was the serious one: the shape change committed, the
    version stamp did not, and every subsequent open re-ran the ALTER and failed on
    "duplicate column name" — permanently, because nothing could get past the failure to
    correct the version.
    """

    @staticmethod
    def _legacy(path: Path) -> None:
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, status TEXT NOT NULL,
                phase TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
                state_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE decisions (
                idempotency_key TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE,
                case_id TEXT NOT NULL, approval_id TEXT NOT NULL, outcome TEXT NOT NULL,
                request_hash TEXT NOT NULL, response_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            INSERT INTO runs VALUES
                ('r1', 'FIN-001', 'COMPLETED', 'COMPLETE', 1, '{}',
                 '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00');
            """
        )
        connection.commit()
        connection.close()

    def test_a_migrated_shape_with_a_lost_version_stamp_still_opens(self, tmp_path: Path) -> None:
        """The reported brick: the ALTER committed and the stamp was lost to a crash."""
        path = tmp_path / "lost_stamp.db"
        self._legacy(path)
        connection = sqlite3.connect(path)
        connection.execute(
            "ALTER TABLE decisions ADD COLUMN invoice_fingerprint TEXT NOT NULL DEFAULT ''"
        )
        connection.commit()
        connection.close()  # the version pragma is left at 0, as a crash would leave it

        with Repository(path) as repository:
            assert repository.schema_version == SCHEMA_VERSION
            assert "invoice_fingerprint" in repository.column_names("decisions")

    def test_a_partially_created_store_is_completed(self, tmp_path: Path) -> None:
        """A first run interrupted inside the schema script leaves some tables and not others.

        Such a store has tables, so it reads as a legacy store, and the migration has no
        ``decisions`` table to alter. The step is skipped and the script creates it complete.
        """
        path = tmp_path / "partial.db"
        connection = sqlite3.connect(path)
        connection.execute(
            "CREATE TABLE runs (run_id TEXT PRIMARY KEY, case_id TEXT NOT NULL,"
            " status TEXT NOT NULL, phase TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,"
            " state_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        connection.commit()
        connection.close()

        with Repository(path) as repository:
            assert "invoice_fingerprint" in repository.column_names("decisions")
            assert repository.schema_version == SCHEMA_VERSION

    def test_a_fresh_store_is_stamped_before_its_shape_exists(self, tmp_path: Path) -> None:
        """Stamping afterwards let a newer build's interrupted create read as a legacy store,
        which an older build would then migrate backwards. The stamp is an upper bound."""
        path = tmp_path / "fresh.db"
        with Repository(path) as repository:
            assert repository.schema_version == SCHEMA_VERSION

    def test_concurrent_first_opens_do_not_collide(self, tmp_path: Path) -> None:
        """Two processes opening an unmigrated store must not both try to migrate it.

        ``BEGIN IMMEDIATE`` takes the write lock before the first statement, so the second
        waits and then finds the work done.
        """
        path = tmp_path / "race.db"
        self._legacy(path)
        errors: list[str] = []

        def open_store() -> None:
            try:
                with Repository(path) as repository:
                    assert repository.schema_version == SCHEMA_VERSION
            except Exception as error:
                errors.append(f"{type(error).__name__}: {error}")

        threads = [threading.Thread(target=open_store) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []
