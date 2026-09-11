"""Tool contract tests.

The reliability behaviours the brief names, each exercised through the same code path a real
outage would take rather than by patching a function: timeout, transient failure with
recovery, permanent failure without retry, and budget exhaustion.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from ap_agent.domain.enums import ToolOutcome, ToolPermission
from ap_agent.domain.errors import (
    PermanentToolError,
    ToolTimeoutError,
    TransientToolError,
)
from ap_agent.domain.request import ProcessingRequest
from ap_agent.domain.run_state import RunState
from ap_agent.observability.events import EventEmitter
from ap_agent.persistence.repository import Repository
from ap_agent.rag.index import CorpusIndex
from ap_agent.rag.ingest import ingest_corpus
from ap_agent.rag.retriever import Retriever
from ap_agent.tools.base import ToolRunner, ToolSpec, describe_tools
from ap_agent.tools.contracts import (
    ALL_TOOL_SPECS,
    CHECK_INVOICE_HISTORY,
    GET_AUTHORITY_DELEGATION,
    GET_PURCHASE_ORDER,
    GET_VENDOR_RECORD,
    RETRIEVE_DOCUMENTS,
    CheckInvoiceHistoryInput,
    CheckInvoiceHistoryOutput,
    GetDelegationInput,
    GetDelegationOutput,
    GetPurchaseOrderInput,
    GetPurchaseOrderOutput,
    GetVendorInput,
    GetVendorOutput,
    RetrieveDocumentsInput,
    RetrieveDocumentsOutput,
    check_invoice_history,
    get_authority_delegation,
    get_purchase_order,
    get_vendor_record,
    retrieve_finance_documents,
)
from ap_agent.tools.mock_backends import MockBackends

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_DIR = REPO_ROOT / "finance_rag_corpus"
AS_OF = datetime(2026, 9, 11, 9, 30, tzinfo=UTC)


class _Probe(BaseModel):
    """Minimal input and output pair for exercising the runner directly."""

    model_config = ConfigDict(extra="forbid")

    value: int = 0


@pytest.fixture
def repository(tmp_path: Path) -> Iterator[Repository]:
    store = Repository(tmp_path / "tools.db")
    yield store
    store.close()


@pytest.fixture
def emitter(repository: Repository) -> EventEmitter:
    state = RunState.create(
        ProcessingRequest(
            case_id="TOOLS",
            invoice_reference="INV-TOOLS",
            vendor="Probe Vendor",
            amount=Decimal("1.00"),
            currency="AUD",
        ),
        now=AS_OF,
    )
    repository.create_run(state)
    return EventEmitter(
        repository=repository, run_id=state.run_id, correlation_id=state.correlation_id
    )


@pytest.fixture
def backends() -> MockBackends:
    return MockBackends(case_id="FIN-001", as_of=AS_OF)


@pytest.fixture(scope="module")
def retriever() -> Retriever:
    return Retriever(CorpusIndex(ingest_corpus(CORPUS_DIR)), superseded_score_factor=0.3)


def _spec(*, timeout: float = 1.0, retries: int = 2) -> ToolSpec:
    return ToolSpec(
        name="probe_tool",
        purpose="Exercise the runner's reliability behaviour.",
        permission=ToolPermission.READ,
        timeout_seconds=timeout,
        max_retries=retries,
        backoff_seconds=0.0,
    )


# ---- declared contracts ------------------------------------------------------------------


class TestDeclaredContracts:
    def test_every_tool_declares_its_full_contract(self) -> None:
        for spec in ALL_TOOL_SPECS:
            assert spec.name
            assert spec.purpose.strip()
            assert spec.permission in {ToolPermission.READ, ToolPermission.WRITE}
            assert spec.timeout_seconds > 0
            assert spec.max_retries >= 0

    def test_attempts_allowed_includes_the_first_attempt(self) -> None:
        assert _spec(retries=2).attempts_allowed == 3
        assert _spec(retries=0).attempts_allowed == 1

    def test_the_manifest_is_generated_from_the_specs(self) -> None:
        """So it cannot describe a tool set other than the one in the code."""
        described = describe_tools(ALL_TOOL_SPECS)
        assert {entry["name"] for entry in described} == {spec.name for spec in ALL_TOOL_SPECS}
        for entry in described:
            assert entry["purpose"]
            assert entry["attempts_allowed"] >= 1

    def test_tool_names_are_unique(self) -> None:
        names = [spec.name for spec in ALL_TOOL_SPECS]
        assert len(names) == len(set(names))


# ---- runner reliability -------------------------------------------------------------------


class TestRunnerReliability:
    def test_a_successful_call_returns_a_validated_value(self, emitter: EventEmitter) -> None:
        runner = ToolRunner(emitter=emitter, max_tool_calls=5)
        result = runner.run(_spec(), lambda _: _Probe(value=7), _Probe(), _Probe)
        assert result.succeeded
        assert result.value is not None
        assert result.value.value == 7
        assert result.attempts == 1

    def test_a_timeout_is_retried_to_the_ceiling_and_no_further(
        self, emitter: EventEmitter, repository: Repository
    ) -> None:
        """Exactly ``max_retries + 1`` attempts. The bound is the point."""
        attempts: list[int] = []

        def slow(_: _Probe) -> _Probe:
            attempts.append(1)
            time.sleep(0.3)
            return _Probe()

        runner = ToolRunner(emitter=emitter, max_tool_calls=10)
        result = runner.run(_spec(timeout=0.05, retries=2), slow, _Probe(), _Probe)
        assert not result.succeeded
        assert result.outcome is ToolOutcome.TIMEOUT
        assert result.attempts == 3
        assert len(attempts) == 3
        assert repository.count_events(emitter.run_id, "TOOL_CALL") == 3

    def test_a_transient_failure_recovers_inside_the_budget(self, emitter: EventEmitter) -> None:
        calls = {"count": 0}

        def flaky(_: _Probe) -> _Probe:
            calls["count"] += 1
            if calls["count"] == 1:
                raise TransientToolError("probe_tool", "simulated 503")
            return _Probe(value=calls["count"])

        runner = ToolRunner(emitter=emitter, max_tool_calls=10)
        result = runner.run(_spec(), flaky, _Probe(), _Probe)
        assert result.succeeded
        assert result.attempts == 2

    def test_a_permanent_failure_is_not_retried(self, emitter: EventEmitter) -> None:
        """Spending the budget on a call that cannot succeed starves a later control."""
        calls = {"count": 0}

        def broken(_: _Probe) -> _Probe:
            calls["count"] += 1
            raise PermanentToolError("probe_tool", "simulated 400")

        runner = ToolRunner(emitter=emitter, max_tool_calls=10)
        result = runner.run(_spec(retries=2), broken, _Probe(), _Probe)
        assert not result.succeeded
        assert result.outcome is ToolOutcome.PERMANENT_ERROR
        assert calls["count"] == 1

    def test_a_response_that_fails_validation_is_permanent(self, emitter: EventEmitter) -> None:
        """A tool whose response does not match its schema is broken, not flaky."""

        class Wrong(BaseModel):
            model_config = ConfigDict(extra="forbid")

            unexpected: str

        calls = {"count": 0}

        def mismatched(_: _Probe) -> Wrong:
            calls["count"] += 1
            return Wrong(unexpected="not a probe")

        runner = ToolRunner(emitter=emitter, max_tool_calls=10)
        result = runner.run(_spec(retries=2), mismatched, _Probe(), _Probe)  # type: ignore[arg-type]
        assert not result.succeeded
        assert result.outcome is ToolOutcome.PERMANENT_ERROR
        assert calls["count"] == 1

    def test_retries_spend_the_budget(self, emitter: EventEmitter) -> None:
        """A retry that cost nothing would make the budget describe an intention."""

        def slow(_: _Probe) -> _Probe:
            time.sleep(0.2)
            return _Probe()

        runner = ToolRunner(emitter=emitter, max_tool_calls=10)
        runner.run(_spec(timeout=0.05, retries=2), slow, _Probe(), _Probe)
        assert runner.calls_used == 3

    def test_an_exhausted_budget_denies_the_call(self, emitter: EventEmitter) -> None:
        runner = ToolRunner(emitter=emitter, max_tool_calls=1)
        first = runner.run(_spec(), lambda _: _Probe(value=1), _Probe(), _Probe)
        second = runner.run(_spec(), lambda _: _Probe(value=2), _Probe(), _Probe)
        assert first.succeeded
        assert not second.succeeded
        assert second.outcome is ToolOutcome.DENIED
        assert second.error_type == "BudgetExhausted"

    def test_a_resumed_runner_continues_the_spent_budget(self, emitter: EventEmitter) -> None:
        """Otherwise a restart would restore the full budget on every attempt."""
        runner = ToolRunner(emitter=emitter, max_tool_calls=3, calls_already_used=2)
        assert runner.budget_remaining == 1
        runner.run(_spec(), lambda _: _Probe(), _Probe(), _Probe)
        assert runner.budget_remaining == 0

    def test_every_attempt_is_recorded_with_a_duration_and_an_outcome(
        self, emitter: EventEmitter, repository: Repository
    ) -> None:
        runner = ToolRunner(emitter=emitter, max_tool_calls=5)
        runner.run(_spec(), lambda _: _Probe(), _Probe(), _Probe)
        events = [
            event
            for event in repository.list_events(emitter.run_id)
            if event.event_type == "TOOL_CALL"
        ]
        assert events
        for event in events:
            assert event.outcome
            assert event.duration_ms is not None
            assert event.payload["tool"] == "probe_tool"
            assert event.payload["attempts_allowed"] == 3


# ---- read tools ---------------------------------------------------------------------------


class TestReadTools:
    def test_the_vendor_tool_returns_a_masked_record(self, backends: MockBackends) -> None:
        output = get_vendor_record(backends)(GetVendorInput(vendor_id="V-1001"))  # type: ignore[operator]
        assert output.found
        assert output.vendor is not None
        assert output.vendor.bank_account_last4 == "4417"
        # There is no field for a full account, so there is nothing to leak.
        assert "bank_account" not in type(output.vendor).model_fields

    def test_an_unknown_vendor_is_a_successful_call_with_no_record(
        self, backends: MockBackends
    ) -> None:
        """Absence is evidence. FIN-POL-001 §5 turns it into a hold, not a crash."""
        output = get_vendor_record(backends)(GetVendorInput(vendor_id="V-NOPE"))  # type: ignore[operator]
        assert not output.found
        assert output.vendor is None

    def test_the_purchase_order_tool_returns_lines_and_receipts(
        self, backends: MockBackends
    ) -> None:
        output = get_purchase_order(backends)(GetPurchaseOrderInput(po_reference="PO-88121"))  # type: ignore[operator]
        assert output.found
        order = output.purchase_order
        assert order is not None
        assert len(order.lines) == 2
        assert len(order.receipts) == 2
        assert order.total_value == Decimal("16320.00")
        assert order.quantity_received_for(1) == Decimal("120")

    def test_the_history_tool_returns_candidates_not_verdicts(self, backends: MockBackends) -> None:
        """Classification is the rule engine's job."""
        output = check_invoice_history(backends)(  # type: ignore[operator]
            CheckInvoiceHistoryInput(
                vendor_id="V-2002",
                invoice_reference="INV-2026-0388",
                currency="AUD",
                gross_amount=Decimal("9240.00"),
            )
        )
        assert output.candidate_count >= 1
        assert set(output.statuses_searched) == {"PAID", "POSTED", "HELD", "REJECTED"}
        assert all(record.match_type == "candidate" for record in output.candidates)

    def test_the_history_tool_only_returns_the_named_vendor(self, backends: MockBackends) -> None:
        output = check_invoice_history(backends)(  # type: ignore[operator]
            CheckInvoiceHistoryInput(
                vendor_id="V-2002",
                invoice_reference="INV-X",
                currency="AUD",
                gross_amount=Decimal("1.00"),
            )
        )
        assert {record.vendor_id for record in output.candidates} == {"V-2002"}

    def test_the_delegation_tool_returns_the_register_entry(self, backends: MockBackends) -> None:
        output = get_authority_delegation(backends)(  # type: ignore[operator]
            GetDelegationInput(delegation_id="DEL-2026-0044")
        )
        assert output.found
        assert output.delegation is not None
        assert output.delegation.delegate_role == "DEPARTMENT_DIRECTOR"
        assert output.register_version == "FIN-POL-003 v4.0"
        assert output.delegation.is_valid_at(AS_OF.date())

    def test_an_expired_delegation_reports_why(self, backends: MockBackends) -> None:
        output = get_authority_delegation(backends)(  # type: ignore[operator]
            GetDelegationInput(delegation_id="DEL-2026-0031")
        )
        assert output.delegation is not None
        assert not output.delegation.is_valid_at(AS_OF.date())
        assert "expired" in (output.delegation.invalid_reason_at(AS_OF.date()) or "")

    def test_a_revoked_delegation_is_invalid_inside_its_dates(self, backends: MockBackends) -> None:
        output = get_authority_delegation(backends)(  # type: ignore[operator]
            GetDelegationInput(delegation_id="DEL-2026-0052")
        )
        assert output.delegation is not None
        assert output.delegation.starts_on <= AS_OF.date() <= output.delegation.ends_on
        assert not output.delegation.is_valid_at(AS_OF.date())

    def test_the_retrieval_tool_returns_ranked_chunks_and_its_query_terms(
        self, retriever: Retriever
    ) -> None:
        output = retrieve_finance_documents(retriever)(  # type: ignore[operator]
            RetrieveDocumentsInput(query="three-way matching tolerance", top_k=3)
        )
        assert output.result_count <= 3
        assert output.query_terms
        assert [chunk.rank for chunk in output.chunks] == list(range(1, output.result_count + 1))


# ---- fault injection -----------------------------------------------------------------------


class TestFaultInjection:
    def test_the_declared_timeout_profile_always_fails(self) -> None:
        backends = MockBackends(case_id="FIN-004", as_of=AS_OF)
        for _ in range(3):
            with pytest.raises(ToolTimeoutError):
                backends.get_purchase_order("PO-99001")
        assert backends.attempt_counts["get_purchase_order"] == 3

    def test_the_transient_profile_succeeds_on_the_second_attempt(self) -> None:
        backends = MockBackends(case_id="FIN-004-TRANSIENT", as_of=AS_OF)
        with pytest.raises(TransientToolError):
            backends.get_purchase_order("PO-99001")
        order = backends.get_purchase_order("PO-99001")
        assert order is not None

    def test_the_permanent_profile_never_succeeds(self) -> None:
        backends = MockBackends(case_id="FIN-004-PERMANENT", as_of=AS_OF)
        with pytest.raises(PermanentToolError):
            backends.get_vendor("V-4004")

    def test_an_unknown_profile_is_refused_at_construction(self, tmp_path: Path) -> None:
        """A typo in a fixture must not turn a fault-injection case into a happy path."""
        import json
        import shutil

        from ap_agent.tools.mock_backends import DEFAULT_MOCK_DATA_DIR

        staging = tmp_path / "mock"
        shutil.copytree(DEFAULT_MOCK_DATA_DIR, staging)
        profiles = json.loads((staging / "fault_profiles.json").read_text(encoding="utf-8"))
        profiles["cases"]["FIN-TYPO"] = {"get_vendor_record": "timout_alwyas"}
        (staging / "fault_profiles.json").write_text(json.dumps(profiles), encoding="utf-8")
        with pytest.raises(ValueError, match="unknown fault profile"):
            MockBackends(data_dir=staging, case_id="FIN-TYPO", as_of=AS_OF)

    def test_a_case_with_no_profile_is_unaffected(self) -> None:
        backends = MockBackends(case_id="FIN-001", as_of=AS_OF)
        assert backends.get_purchase_order("PO-88121") is not None
        assert backends.attempt_counts == {}


class TestRelativeFixtureDates:
    """Relative dates keep a fixture's intent stable rather than its values."""

    def test_the_bank_change_stays_recent_whenever_the_case_runs(self) -> None:
        for year in (2026, 2030, 2040):
            moment = datetime(year, 6, 15, tzinfo=UTC)
            backends = MockBackends(case_id="FIN-003", as_of=moment)
            vendor = backends.get_vendor("V-3003")
            assert vendor is not None
            assert vendor.bank_changed_recently(moment), year

    def test_the_long_standing_vendor_is_never_new(self) -> None:
        for year in (2026, 2035):
            moment = datetime(year, 6, 15, tzinfo=UTC)
            vendor = MockBackends(case_id="FIN-001", as_of=moment).get_vendor("V-1001")
            assert vendor is not None
            assert not vendor.is_new_vendor(moment), year

    def test_the_valid_delegation_stays_valid(self) -> None:
        for year in (2026, 2032):
            moment = datetime(year, 3, 3, tzinfo=UTC)
            delegation = MockBackends(case_id="FIN-005", as_of=moment).get_delegation(
                "DEL-2026-0044"
            )
            assert delegation is not None
            assert delegation.is_valid_at(moment.date()), year

    def test_the_expired_delegation_stays_expired(self) -> None:
        for year in (2026, 2032):
            moment = datetime(year, 3, 3, tzinfo=UTC)
            delegation = MockBackends(case_id="FIN-005", as_of=moment).get_delegation(
                "DEL-2026-0031"
            )
            assert delegation is not None
            assert not delegation.is_valid_at(moment.date()), year


# ---- input validation -----------------------------------------------------------------------


class TestInputValidation:
    def test_an_empty_identifier_is_rejected(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            GetVendorInput(vendor_id="")

    def test_an_unexpected_argument_is_rejected(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            GetPurchaseOrderInput(po_reference="PO-1", force=True)  # type: ignore[call-arg]

    def test_top_k_is_bounded(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            RetrieveDocumentsInput(query="x", top_k=999)

    def test_a_float_amount_is_rejected_on_a_tool_input(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="must not be floats"):
            CheckInvoiceHistoryInput(
                vendor_id="V-1",
                invoice_reference="INV-1",
                currency="AUD",
                gross_amount=100.00,  # type: ignore[arg-type]
            )


# ---- output schema coverage ------------------------------------------------------------------


class TestOutputSchemas:
    @pytest.mark.parametrize(
        ("spec", "output_model"),
        [
            (RETRIEVE_DOCUMENTS, RetrieveDocumentsOutput),
            (GET_VENDOR_RECORD, GetVendorOutput),
            (GET_PURCHASE_ORDER, GetPurchaseOrderOutput),
            (CHECK_INVOICE_HISTORY, CheckInvoiceHistoryOutput),
            (GET_AUTHORITY_DELEGATION, GetDelegationOutput),
        ],
    )
    def test_every_read_tool_has_an_explicit_output_schema(
        self, spec: ToolSpec, output_model: type[BaseModel]
    ) -> None:
        assert spec.permission is ToolPermission.READ
        assert output_model.model_fields

    def test_read_outputs_carry_a_retrieval_timestamp(self) -> None:
        """So a staleness question can be answered from the record itself."""
        for model in (GetVendorOutput, GetPurchaseOrderOutput, CheckInvoiceHistoryOutput):
            assert "retrieved_at" in model.model_fields
