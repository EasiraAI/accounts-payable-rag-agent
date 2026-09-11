"""The five required cases, run end to end through the shared evaluation runner.

Deliberately thin. The assertions live in the fixture files, and the runner checks them, so
these tests verify that the runner reaches the right verdict rather than restating the
expectations a third time. Anything asserted here and not in a fixture would be a rule the
command line and the HTTP endpoint do not enforce.

Runs against the deterministic adapter. The live tier is the same cases with
``--provider anthropic`` and is marked ``live_model``, excluded by default.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ap_agent.config.settings import Settings
from ap_agent.evaluation.cases import REQUIRED_CASE_IDS, load_cases
from ap_agent.evaluation.runner import EvaluationReport, run_evaluation

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_DIR = REPO_ROOT / "finance_rag_corpus"


@pytest.fixture(scope="module")
def report(tmp_path_factory: pytest.TempPathFactory) -> EvaluationReport:
    """One evaluation run shared by the whole module.

    Module-scoped because the run is the expensive part and every test reads the same result.
    Each case still gets its own database inside the runner, so they remain isolated from one
    another.
    """
    workspace = tmp_path_factory.mktemp("fixture-eval")
    settings = Settings(
        llm_provider="fake",
        corpus_dir=CORPUS_DIR,
        index_dir=workspace / "index",
        db_path=workspace / "unused.db",
        tool_timeout_seconds=0.5,
        tool_max_retries=2,
        tool_retry_backoff_seconds=0.0,
    )
    return run_evaluation(settings=settings, workspace=workspace / "runs")


class TestFixtureCoverage:
    def test_all_five_required_cases_are_present(self) -> None:
        assert {case.case_id for case in load_cases()} >= set(REQUIRED_CASE_IDS)

    def test_a_missing_required_case_is_an_error(self, tmp_path: Path) -> None:
        """An evaluation run that silently covers fewer cases proves less than it appears to."""
        import json
        import shutil

        from ap_agent.evaluation.cases import DEFAULT_CASES_DIR

        staging = tmp_path / "cases"
        staging.mkdir()
        for source in DEFAULT_CASES_DIR.glob("FIN-*.json"):
            if source.stem != "FIN-005":
                shutil.copy(source, staging / source.name)
        with pytest.raises(FileNotFoundError, match="FIN-005"):
            load_cases(staging)
        # Guard against the fixture directory itself being the cause.
        assert (
            json.loads((DEFAULT_CASES_DIR / "FIN-005.json").read_text(encoding="utf-8"))["request"][
                "case_id"
            ]
            == "FIN-005"
        )

    def test_every_case_states_its_intent(self) -> None:
        """A fixture a reviewer cannot read is a fixture nobody maintains."""
        for case in load_cases():
            assert len(case.intent) > 40, case.case_id


class TestAllCasesPass:
    def test_every_case_passes(self, report: EvaluationReport) -> None:
        failures = {
            result.case_id: result.failures for result in report.results if not result.passed
        }
        assert failures == {}, report.table()

    def test_the_report_covers_the_required_cases(self, report: EvaluationReport) -> None:
        assert {result.case_id for result in report.results} >= set(REQUIRED_CASE_IDS)

    def test_the_report_is_produced_by_the_deterministic_adapter(
        self, report: EvaluationReport
    ) -> None:
        assert report.provider == "fake"

    @pytest.mark.parametrize("case_id", REQUIRED_CASE_IDS)
    def test_each_case_individually(self, report: EvaluationReport, case_id: str) -> None:
        """Named per case so a failure identifies itself in the test output."""
        result = next(item for item in report.results if item.case_id == case_id)
        assert result.passed, "\n".join(result.failures)


class TestObservedBehaviour:
    """A few cross-cutting properties of the run, not restatements of the fixtures."""

    def test_every_case_stays_inside_its_budgets(self, report: EvaluationReport) -> None:
        for result in report.results:
            assert result.steps_used <= 12, result.case_id
            assert result.tool_calls_used <= 16, result.case_id

    def test_only_the_two_approved_cases_record_a_decision(self, report: EvaluationReport) -> None:
        recorded = {result.case_id for result in report.results if result.decision_count > 0}
        assert recorded == {"FIN-001", "FIN-005"}

    def test_every_case_produces_cited_evidence(self, report: EvaluationReport) -> None:
        """A recommendation with no citations is not grounded, whatever it concludes."""
        for result in report.results:
            assert result.citation_count > 0, result.case_id

    def test_only_the_poisoned_case_carries_an_injection_in_its_own_content(
        self, report: EvaluationReport
    ) -> None:
        """The distinction the design turns on, asserted rather than assumed.

        An injection found in the *case* (notes or attachments) is evidence about this
        transaction and feeds the indicator count. An injection found in a *retrieved corpus
        document* is a corpus hygiene observation and feeds nothing, because a document merely
        present in the corpus says nothing about the invoice in hand.

        FIN-002 legitimately logs the second kind: its evidence search surfaces the
        adversarial supplier notice, which concerns a different vendor entirely. An earlier
        revision of this test conflated the two and failed on correct behaviour.
        """
        case_level = {
            result.case_id for result in report.results if result.injection_events_case > 0
        }
        assert case_level == {"FIN-003"}

    def test_a_corpus_level_injection_does_not_contribute_indicators(
        self, report: EvaluationReport
    ) -> None:
        corpus_level = next(result for result in report.results if result.case_id == "FIN-002")
        assert corpus_level.injection_events_corpus > 0, (
            "the evidence search should have surfaced and screened the adversarial notice"
        )
        assert corpus_level.fraud_indicators == [], (
            "a document about another supplier must not raise indicators against this case"
        )
        assert corpus_level.actual == "REJECT_DUPLICATE"

    def test_the_missing_evidence_case_is_the_one_with_unknowns(
        self, report: EvaluationReport
    ) -> None:
        """Every other case has complete evidence, so an unknown elsewhere would be a gap
        the fixtures did not intend."""
        with_unknowns = {result.case_id for result in report.results if result.unknown_count > 0}
        assert with_unknowns == {"FIN-004"}

    def test_the_run_is_fast_enough_to_be_used_interactively(
        self, report: EvaluationReport
    ) -> None:
        """Not a benchmark. A guard: if a case starts taking seconds against simulated
        backends, something is retrying or sleeping when it should not be."""
        for result in report.results:
            assert result.duration_ms < 5_000, f"{result.case_id} took {result.duration_ms}ms"


class TestDeterminism:
    def test_two_runs_of_the_same_cases_agree(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """The same inputs must produce the same outcomes, or a run is not reproducible.

        Identifiers and timestamps differ between runs by design; the outcomes, exceptions and
        indicators must not.
        """
        workspace = tmp_path_factory.mktemp("determinism")
        settings = Settings(
            llm_provider="fake",
            corpus_dir=CORPUS_DIR,
            index_dir=workspace / "index",
            db_path=workspace / "unused.db",
            tool_timeout_seconds=0.5,
            tool_max_retries=2,
            tool_retry_backoff_seconds=0.0,
        )
        first = run_evaluation(settings=settings, workspace=workspace / "a")
        second = run_evaluation(settings=settings, workspace=workspace / "b")

        def fingerprint(report: EvaluationReport) -> list[tuple[str, str, list[str], list[str]]]:
            return [
                (
                    result.case_id,
                    result.actual,
                    result.exception_categories,
                    result.fraud_indicators,
                )
                for result in report.results
            ]

        assert fingerprint(first) == fingerprint(second)


class TestTranscripts:
    def test_a_transcript_is_written_for_every_case(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """The documentation samples are generated from the run that reports pass or fail, so
        they cannot describe behaviour the tests did not observe."""
        import json

        workspace = tmp_path_factory.mktemp("transcripts")
        settings = Settings(
            llm_provider="fake",
            corpus_dir=CORPUS_DIR,
            index_dir=workspace / "index",
            db_path=workspace / "unused.db",
            tool_timeout_seconds=0.5,
            tool_max_retries=2,
            tool_retry_backoff_seconds=0.0,
        )
        transcripts = workspace / "transcripts"
        run_evaluation(settings=settings, workspace=workspace / "runs", transcript_dir=transcripts)
        written = sorted(path.name for path in transcripts.glob("*_transcript.json"))
        assert written == [f"{case_id}_transcript.json" for case_id in REQUIRED_CASE_IDS]

        for path in transcripts.glob("*_transcript.json"):
            payload = json.loads(path.read_text(encoding="utf-8"))
            assert payload["summary"]["run_id"]
            assert payload["events"]
            assert payload["intent"]
            # A transcript is a deliverable, so it must not carry anything a log may not.
            blob = json.dumps(payload)
            for marker in ("sk-ant", "ANTHROPIC_API_KEY"):
                assert marker not in blob


@pytest.mark.live_model
class TestLiveModelTier:
    """The same cases through a real model. Excluded by default; needs external access."""

    def test_every_case_passes_with_the_live_provider(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        import os

        if not os.environ.get("ANTHROPIC_API_KEY"):
            pytest.skip("ANTHROPIC_API_KEY is not set")

        from ap_agent.llm import build_llm_client

        workspace = tmp_path_factory.mktemp("live-eval")
        settings = Settings(
            llm_provider="anthropic",
            corpus_dir=CORPUS_DIR,
            index_dir=workspace / "index",
            db_path=workspace / "unused.db",
            tool_timeout_seconds=2.0,
            tool_max_retries=2,
            tool_retry_backoff_seconds=0.0,
        )
        report = run_evaluation(
            settings=settings,
            llm_client=build_llm_client(settings),
            workspace=workspace / "runs",
        )
        assert report.all_passed, report.table()
