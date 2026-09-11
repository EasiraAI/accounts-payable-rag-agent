"""HTTP contract tests.

Exercised through FastAPI's test client against a real application instance built on a
temporary database and the deterministic model adapter. Nothing is patched, so what these
tests assert is what a caller over the network would see.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ap_agent.api.app import create_app
from ap_agent.composition import build_application
from ap_agent.config.settings import Settings
from ap_agent.llm.fake_client import FakeLLMClient

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_DIR = REPO_ROOT / "finance_rag_corpus"
CASES_DIR = REPO_ROOT / "fixtures" / "cases"
AS_OF = datetime(2026, 9, 11, 9, 30, tzinfo=UTC)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        llm_provider="fake",
        corpus_dir=CORPUS_DIR,
        index_dir=tmp_path / "index",
        db_path=tmp_path / "runtime" / "api.db",
        tool_timeout_seconds=0.5,
        tool_max_retries=2,
        tool_retry_backoff_seconds=0.0,
    )
    application = build_application(
        settings=settings,
        llm_client=FakeLLMClient(),
        clock=lambda: AS_OF,
        configure_logs=False,
    )
    with TestClient(create_app(application)) as test_client:
        yield test_client
    application.close()


def _request_body(case_id: str) -> dict[str, Any]:
    payload = json.loads((CASES_DIR / f"{case_id}.json").read_text(encoding="utf-8"))
    body: dict[str, Any] = payload["request"]
    return body


# ---- start run ---------------------------------------------------------------------------


class TestStartRun:
    def test_a_clean_case_returns_201_and_stops_at_the_gate(self, client: TestClient) -> None:
        response = client.post("/runs", json=_request_body("FIN-001"))
        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "AWAITING_APPROVAL"
        assert body["recommendation"]["outcome"] == "APPROVE_FOR_POSTING"
        assert body["decision"] is None
        assert body["pending_approval"] is not None

    def test_the_response_carries_both_output_shapes(self, client: TestClient) -> None:
        """The brief asks for a recommendation and a typed result with distinct fields."""
        body = client.post("/runs", json=_request_body("FIN-001")).json()
        recommendation = body["recommendation"]
        for field in (
            "cited_evidence",
            "calculations",
            "assumptions",
            "confidence",
            "exceptions",
            "next_action",
        ):
            assert field in recommendation, field
        result = body["result"]
        for field in (
            "sourced_facts",
            "calculations",
            "inferences",
            "unknowns",
            "policy_findings",
            "actions_taken",
        ):
            assert field in result, field

    def test_the_response_carries_the_audit_events(self, client: TestClient) -> None:
        body = client.post("/runs", json=_request_body("FIN-001")).json()
        events = body["events"]
        assert events
        types = {event["event_type"] for event in events}
        assert "RUN_CREATED" in types
        assert "APPROVAL_REQUESTED" in types
        for event in events:
            assert event["sequence"] >= 1
            assert event["created_at"]

    def test_calculations_are_serialised_as_exact_decimals(self, client: TestClient) -> None:
        body = client.post("/runs", json=_request_body("FIN-001")).json()
        calculations = body["recommendation"]["calculations"]
        assert calculations
        for calculation in calculations:
            assert isinstance(calculation["result"], str), (
                "a monetary result must not cross the wire as a float"
            )
            assert calculation["formula"]
            assert calculation["policy_ref"]
            assert calculation["rounding"]

    def test_a_malformed_request_is_rejected_before_a_run_is_created(
        self, client: TestClient
    ) -> None:
        response = client.post("/runs", json={"case_id": "X"})
        assert response.status_code == 422
        assert client.get("/runs").json() == []

    def test_an_unexpected_field_is_rejected(self, client: TestClient) -> None:
        body = _request_body("FIN-001") | {"approve_immediately": True}
        assert client.post("/runs", json=body).status_code == 422

    def test_a_float_amount_is_rejected(self, client: TestClient) -> None:
        """JSON has no decimal type, so an amount must arrive as a string."""
        body = _request_body("FIN-001") | {"amount": 17952.00}
        response = client.post("/runs", json=body)
        assert response.status_code == 422
        assert "float" in response.text.lower()


# ---- get run -----------------------------------------------------------------------------


class TestGetRun:
    def test_a_run_can_be_read_back(self, client: TestClient) -> None:
        run_id = client.post("/runs", json=_request_body("FIN-001")).json()["run_id"]
        response = client.get(f"/runs/{run_id}")
        assert response.status_code == 200
        assert response.json()["run_id"] == run_id

    def test_an_unknown_run_is_404(self, client: TestClient) -> None:
        response = client.get("/runs/run_missing")
        assert response.status_code == 404
        assert "not found" in response.json()["detail"]

    def test_runs_are_listable(self, client: TestClient) -> None:
        client.post("/runs", json=_request_body("FIN-001"))
        client.post("/runs", json=_request_body("FIN-004"))
        listing = client.get("/runs").json()
        assert len(listing) == 2
        assert {row["case_id"] for row in listing} == {"FIN-001", "FIN-004"}


# ---- approve and reject --------------------------------------------------------------------


class TestApproval:
    def _start_and_approval(self, client: TestClient, case_id: str) -> tuple[str, str]:
        body = client.post("/runs", json=_request_body(case_id)).json()
        return body["run_id"], body["pending_approval"]["approval_id"]

    def test_approving_completes_the_run_and_records_one_decision(self, client: TestClient) -> None:
        run_id, approval_id = self._start_and_approval(client, "FIN-001")
        response = client.post(
            f"/runs/{run_id}/approve",
            json={
                "approval_id": approval_id,
                "approver_id": "U-3081",
                "approver_role": "DEPARTMENT_DIRECTOR",
                "comment": "Matched and receipted.",
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["replayed"] is False
        assert body["run"]["status"] == "COMPLETED"
        assert body["run"]["decision"]["simulated"] is True
        assert body["run"]["decision"]["posting_system"] == "SIMULATED_ERP"

    def test_a_duplicate_delivery_returns_200_with_an_identical_decision(
        self, client: TestClient
    ) -> None:
        """200 rather than 409: a duplicate delivery is a success in an at-least-once world."""
        run_id, approval_id = self._start_and_approval(client, "FIN-001")
        payload = {
            "approval_id": approval_id,
            "approver_id": "U-3081",
            "approver_role": "DEPARTMENT_DIRECTOR",
        }
        first = client.post(f"/runs/{run_id}/approve", json=payload).json()
        second = client.post(f"/runs/{run_id}/approve", json=payload).json()
        assert first["replayed"] is False
        assert second["replayed"] is True
        assert second["run"]["decision"] == first["run"]["decision"]

    def test_three_deliveries_still_record_one_decision(self, client: TestClient) -> None:
        run_id, approval_id = self._start_and_approval(client, "FIN-001")
        payload = {
            "approval_id": approval_id,
            "approver_id": "U-3081",
            "approver_role": "DEPARTMENT_DIRECTOR",
        }
        references = set()
        for _ in range(3):
            body = client.post(f"/runs/{run_id}/approve", json=payload).json()
            references.add(body["run"]["decision"]["decision_ref"])
        assert len(references) == 1
        events = client.get(f"/runs/{run_id}").json()["events"]
        submitted = [e for e in events if e["event_type"] == "DECISION_SUBMITTED"]
        replayed = [e for e in events if e["event_type"] == "APPROVAL_REPLAYED"]
        assert len(submitted) == 1
        assert len(replayed) == 2

    def test_rejecting_holds_the_run_and_records_no_decision(self, client: TestClient) -> None:
        run_id, approval_id = self._start_and_approval(client, "FIN-001")
        body = client.post(
            f"/runs/{run_id}/reject",
            json={
                "approval_id": approval_id,
                "approver_id": "U-3081",
                "approver_role": "DEPARTMENT_DIRECTOR",
                "comment": "Receipt evidence is not convincing.",
            },
        ).json()
        assert body["run"]["status"] == "HELD"
        assert body["run"]["decision"] is None

    def test_approving_after_rejecting_is_409(self, client: TestClient) -> None:
        run_id, approval_id = self._start_and_approval(client, "FIN-001")
        payload = {
            "approval_id": approval_id,
            "approver_id": "U-3081",
            "approver_role": "DEPARTMENT_DIRECTOR",
        }
        client.post(f"/runs/{run_id}/reject", json=payload)
        response = client.post(f"/runs/{run_id}/approve", json=payload)
        assert response.status_code == 409
        assert "REJECTED" in response.json()["detail"]

    def test_an_insufficient_approver_is_409_and_records_nothing(self, client: TestClient) -> None:
        run_id, approval_id = self._start_and_approval(client, "FIN-001")
        response = client.post(
            f"/runs/{run_id}/approve",
            json={
                "approval_id": approval_id,
                "approver_id": "U-9001",
                "approver_role": "COST_CENTRE_MANAGER",
            },
        )
        assert response.status_code == 409
        assert "authority" in response.json()["detail"]
        assert client.get(f"/runs/{run_id}").json()["decision"] is None

    def test_a_fabricated_approval_id_is_409(self, client: TestClient) -> None:
        run_id, _ = self._start_and_approval(client, "FIN-001")
        response = client.post(
            f"/runs/{run_id}/approve",
            json={
                "approval_id": "apr_fabricated",
                "approver_id": "U-3081",
                "approver_role": "DEPARTMENT_DIRECTOR",
            },
        )
        assert response.status_code == 409
        assert client.get(f"/runs/{run_id}").json()["decision"] is None

    def test_approving_a_held_run_is_409(self, client: TestClient) -> None:
        """FIN-004 never reaches the gate, so there is nothing to approve."""
        run_id = client.post("/runs", json=_request_body("FIN-004")).json()["run_id"]
        response = client.post(
            f"/runs/{run_id}/approve",
            json={
                "approval_id": "apr_anything",
                "approver_id": "U-3081",
                "approver_role": "DEPARTMENT_DIRECTOR",
            },
        )
        assert response.status_code == 409

    def test_an_approval_without_an_identifier_is_422(self, client: TestClient) -> None:
        run_id, _ = self._start_and_approval(client, "FIN-001")
        response = client.post(
            f"/runs/{run_id}/approve",
            json={"approver_id": "U-3081", "approver_role": "DEPARTMENT_DIRECTOR"},
        )
        assert response.status_code == 422

    def test_an_approval_under_a_valid_delegation_succeeds(self, client: TestClient) -> None:
        run_id, approval_id = self._start_and_approval(client, "FIN-005")
        body = client.post(
            f"/runs/{run_id}/approve",
            json={
                "approval_id": approval_id,
                "approver_id": "U-7781",
                "approver_role": "COST_CENTRE_MANAGER",
                "delegation_id": "DEL-2026-0044",
            },
        ).json()
        assert body["run"]["status"] == "COMPLETED"
        events = client.get(f"/runs/{run_id}").json()["events"]
        resolved = [e for e in events if e["event_type"] == "APPROVAL_RESOLVED"][-1]
        assert resolved["payload"]["delegation_applied"] == "DEL-2026-0044"
        assert resolved["payload"]["effective_role"] == "DEPARTMENT_DIRECTOR"

    def test_an_approval_under_an_expired_delegation_is_409(self, client: TestClient) -> None:
        run_id, approval_id = self._start_and_approval(client, "FIN-005")
        response = client.post(
            f"/runs/{run_id}/approve",
            json={
                "approval_id": approval_id,
                "approver_id": "U-7781",
                "approver_role": "COST_CENTRE_MANAGER",
                "delegation_id": "DEL-2026-0031",
            },
        )
        assert response.status_code == 409
        assert "expired" in response.json()["detail"].lower()


# ---- non-consequential outcomes -------------------------------------------------------------


class TestNonConsequentialOutcomes:
    def test_a_held_case_creates_no_approval(self, client: TestClient) -> None:
        body = client.post("/runs", json=_request_body("FIN-004")).json()
        assert body["status"] == "HELD"
        assert body["pending_approval"] is None
        assert body["decision"] is None

    def test_an_escalated_case_creates_no_approval(self, client: TestClient) -> None:
        body = client.post("/runs", json=_request_body("FIN-003")).json()
        assert body["recommendation"]["outcome"] == "ESCALATE_CONTROL_REVIEW"
        assert body["pending_approval"] is None

    def test_a_duplicate_rejection_is_gated_like_any_consequential_outcome(
        self, client: TestClient
    ) -> None:
        """Rejecting writes to the ledger of record, so it needs an approver too."""
        body = client.post("/runs", json=_request_body("FIN-002")).json()
        assert body["recommendation"]["outcome"] == "REJECT_DUPLICATE"
        assert body["recommendation"]["requires_approval"] is True
        assert body["pending_approval"] is not None
        assert body["decision"] is None


# ---- diagnostics -----------------------------------------------------------------------------


class TestDiagnostics:
    def test_health_reports_what_the_instance_is_made_of(self, client: TestClient) -> None:
        body = client.get("/health").json()
        assert body["status"] == "ok"
        assert body["provider"] == "fake"
        assert body["corpus_documents"] == 15
        assert body["corpus_chunks"] == 58

    def test_the_manifest_describes_tools_and_trust_boundaries(self, client: TestClient) -> None:
        body = client.get("/manifest").json()
        assert {tool["name"] for tool in body["tools"]} == {
            "retrieve_finance_documents",
            "get_vendor_record",
            "get_purchase_order",
            "check_invoice_history",
            "get_authority_delegation",
            "submit_finance_decision",
        }
        writers = [tool for tool in body["tools"] if tool["permission"] == "WRITE"]
        assert [tool["name"] for tool in writers] == ["submit_finance_decision"]
        assert body["trust_boundaries"]["untrusted_case_input"]
        assert body["trust_boundaries"]["enforcement"]

    def test_the_manifest_contains_no_credential(self, client: TestClient) -> None:
        text = client.get("/manifest").text
        for marker in ("sk-ant", "ANTHROPIC_API_KEY", "api_key"):
            assert marker not in text

    def test_the_openapi_schema_is_served(self, client: TestClient) -> None:
        schema = client.get("/openapi.json").json()
        for path in ("/runs", "/runs/{run_id}", "/runs/{run_id}/approve", "/evaluations"):
            assert path in schema["paths"], path


class TestEvaluationEndpoint:
    def test_the_endpoint_runs_every_case_and_they_all_pass(self, client: TestClient) -> None:
        body = client.get("/evaluations").json()
        assert body["case_count"] == 5
        assert body["passed_count"] == 5
        assert {result["case_id"] for result in body["results"]} == {
            "FIN-001",
            "FIN-002",
            "FIN-003",
            "FIN-004",
            "FIN-005",
        }

    def test_the_endpoint_always_uses_the_deterministic_adapter(self, client: TestClient) -> None:
        """An endpoint that spent live model quota on each call would be used once."""
        assert client.get("/evaluations").json()["provider"] == "fake"

    def test_evaluation_runs_do_not_pollute_the_service_database(self, client: TestClient) -> None:
        client.get("/evaluations")
        assert client.get("/runs").json() == []
