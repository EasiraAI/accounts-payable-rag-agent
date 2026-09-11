"""Command-line surface.

Mirrors the HTTP operations so the system is usable without starting a server, and adds the
two commands a server has no use for: building the index, and printing the manifest.

Both transports call the same composition root and the same orchestrator. Nothing here
interprets policy or makes a decision; the commands parse arguments, call in, and format what
comes back.

Exit codes matter for a command that runs in continuous integration: ``eval`` returns 1 when
any case fails, so a pipeline step fails without having to parse the table.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import typer

from ap_agent.composition import build_application
from ap_agent.config.settings import Settings, get_settings
from ap_agent.domain.request import ApprovalDecision, ProcessingRequest
from ap_agent.evaluation.retrieval import evaluate_retrieval, load_golden_set
from ap_agent.evaluation.runner import run_evaluation
from ap_agent.llm import build_llm_client
from ap_agent.orchestration.machine import build_final_result, summarise_run
from ap_agent.rag.index import build_index
from ap_agent.tools.base import describe_tools
from ap_agent.tools.contracts import ALL_TOOL_SPECS

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help=(
        "Accounts-payable processing agent. Reconciles invoice evidence against policy, "
        "produces a cited recommendation, and stops at a human approval gate. All posting is "
        "simulated."
    ),
)

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_GOLDEN_SET = REPO_ROOT / "tests" / "eval" / "retrieval_golden.json"


def _settings(provider: str | None) -> Settings:
    """Settings from the environment, with the provider optionally overridden.

    An explicit ``--provider`` on the command line beats the environment, which is what makes
    ``--provider fake`` a reliable way to run without model access whatever is configured.
    """
    base = get_settings()
    if provider is None:
        return base
    return base.model_copy(update={"llm_provider": provider})


def _echo_json(payload: object) -> None:
    typer.echo(json.dumps(payload, indent=2, default=str))


@app.command()
def ingest(
    force: Annotated[bool, typer.Option(help="Rebuild even if the corpus is unchanged.")] = False,
) -> None:
    """Chunk and index the policy corpus.

    Idempotent: an unchanged corpus produces the same hash and the existing index is reused,
    so this is safe to run on every start.
    """
    settings = get_settings()
    index, rebuilt = build_index(settings.corpus_dir, settings.index_dir, force=force)
    typer.echo(
        f"{'rebuilt' if rebuilt else 'up to date'}: {index.document_count} document(s), "
        f"{len(index)} chunk(s), corpus hash {index.corpus_hash[:16]}"
    )
    typer.echo(f"index directory: {settings.index_dir}")


@app.command()
def run(
    case_file: Annotated[Path, typer.Argument(help="JSON file containing a processing request.")],
    provider: Annotated[str | None, typer.Option(help="Override AP_LLM_PROVIDER.")] = None,
    full: Annotated[bool, typer.Option(help="Print the whole run, not a summary.")] = False,
) -> None:
    """Start a run from a processing request and execute it to the approval gate.

    The file may be a bare processing request or a fixture case with a ``request`` key, so a
    fixture can be run directly without being unwrapped first.
    """
    payload = json.loads(case_file.read_text(encoding="utf-8"))
    request = ProcessingRequest.model_validate(payload.get("request", payload))
    with build_application(settings=_settings(provider)) as application:
        state = application.orchestrator.start(request)
        if full:
            _echo_json(state.model_dump(mode="json"))
        else:
            _echo_json(summarise_run(state))
            if state.recommendation is not None:
                typer.echo("")
                typer.echo(f"outcome:     {state.recommendation.outcome.value}")
                typer.echo(f"summary:     {state.recommendation.summary}")
                typer.echo(f"next action: {state.recommendation.next_action}")
                typer.echo(
                    f"confidence:  {state.recommendation.confidence.score} "
                    f"({state.recommendation.confidence.basis})"
                )
                if state.approval_id:
                    typer.echo("")
                    typer.echo(
                        f"Approval required. Resolve with:\n"
                        f"  ap-agent approve {state.run_id} --approval-id {state.approval_id} "
                        f"--approver-id U-3081 --approver-role DEPARTMENT_DIRECTOR"
                    )


@app.command()
def get(
    run_id: Annotated[str, typer.Argument(help="Run identifier.")],
    events: Annotated[bool, typer.Option(help="Include the audit event log.")] = False,
) -> None:
    """Show a run's status, result and audit events."""
    with build_application() as application:
        state = application.repository.require_run(run_id)
        payload: dict[str, object] = {
            "summary": summarise_run(state),
            "recommendation": (
                state.recommendation.model_dump(mode="json") if state.recommendation else None
            ),
            "result": (
                result.model_dump(mode="json")
                if (result := build_final_result(state)) is not None
                else None
            ),
        }
        if events:
            payload["events"] = [
                {
                    "sequence": event.sequence,
                    "event_type": event.event_type,
                    "phase": event.phase,
                    "outcome": event.outcome,
                    "duration_ms": event.duration_ms,
                    "payload": event.payload,
                }
                for event in application.repository.list_events(run_id)
            ]
        _echo_json(payload)


def _resolve(
    run_id: str,
    approval_id: str,
    approver_id: str,
    approver_role: str,
    comment: str,
    delegation_id: str | None,
    provider: str | None,
    *,
    approve_it: bool,
) -> None:
    decision = ApprovalDecision(
        approval_id=approval_id,
        approver_id=approver_id,
        approver_role=approver_role,
        comment=comment,
        delegation_id=delegation_id,
    )
    with build_application(settings=_settings(provider)) as application:
        action = application.orchestrator.approve if approve_it else application.orchestrator.reject
        state, replayed = action(run_id, decision)
        _echo_json(
            {
                "replayed": replayed,
                "summary": summarise_run(state),
                "decision": state.decision.model_dump(mode="json") if state.decision else None,
                "actions_taken": [
                    action_record.model_dump(mode="json") for action_record in state.actions_taken
                ],
            }
        )
        if replayed:
            typer.echo(
                "\nThis delivery was a replay: the decision was already recorded and nothing "
                "was repeated."
            )


@app.command()
def approve(
    run_id: Annotated[str, typer.Argument()],
    approval_id: Annotated[str, typer.Option(help="Approval identifier from the run.")],
    approver_id: Annotated[str, typer.Option(help="Identifier of the approving person.")],
    approver_role: Annotated[str, typer.Option(help="Role from the FIN-POL-003 §2 matrix.")],
    comment: Annotated[str, typer.Option()] = "",
    delegation_id: Annotated[str | None, typer.Option(help="Authority register entry.")] = None,
    provider: Annotated[str | None, typer.Option(help="Override AP_LLM_PROVIDER.")] = None,
) -> None:
    """Approve a pending decision and resume the run. Safe to repeat."""
    _resolve(
        run_id,
        approval_id,
        approver_id,
        approver_role,
        comment,
        delegation_id,
        provider,
        approve_it=True,
    )


@app.command()
def reject(
    run_id: Annotated[str, typer.Argument()],
    approval_id: Annotated[str, typer.Option(help="Approval identifier from the run.")],
    approver_id: Annotated[str, typer.Option(help="Identifier of the deciding person.")],
    approver_role: Annotated[str, typer.Option(help="Role from the FIN-POL-003 §2 matrix.")],
    comment: Annotated[str, typer.Option()] = "",
    delegation_id: Annotated[str | None, typer.Option()] = None,
    provider: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Reject a pending decision. Nothing is posted and the case is held."""
    _resolve(
        run_id,
        approval_id,
        approver_id,
        approver_role,
        comment,
        delegation_id,
        provider,
        approve_it=False,
    )


@app.command(name="eval")
def evaluate(
    provider: Annotated[
        str, typer.Option(help="fake (default, deterministic) or anthropic (needs a key).")
    ] = "fake",
    transcripts: Annotated[
        Path | None, typer.Option(help="Directory to write per-case transcripts into.")
    ] = None,
    report: Annotated[Path | None, typer.Option(help="Write the report as JSON.")] = None,
    retrieval: Annotated[bool, typer.Option(help="Also measure retrieval quality.")] = True,
) -> None:
    """Run the fixture cases and report pass or fail per case.

    Exits non-zero if any case fails or any retrieval gate is missed, so this is usable as a
    pipeline step without parsing the output.
    """
    settings = _settings(provider)
    client = build_llm_client(settings)
    result = run_evaluation(settings=settings, llm_client=client, transcript_dir=transcripts)
    typer.echo(result.table())

    retrieval_ok = True
    if retrieval and DEFAULT_GOLDEN_SET.is_file():
        with build_application(settings=settings, llm_client=client) as application:
            retrieval_report = evaluate_retrieval(
                application.retriever,
                load_golden_set(DEFAULT_GOLDEN_SET),
                top_k=settings.retrieval_top_k,
            )
        typer.echo("")
        typer.echo(retrieval_report.summary_line())
        retrieval_ok = retrieval_report.gates_passed
        if report is not None:
            payload = {
                "cases": result.model_dump(mode="json"),
                "retrieval": retrieval_report.model_dump(mode="json"),
            }
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
            typer.echo(f"\nreport written to {report}")
    elif report is not None:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(
            json.dumps({"cases": result.model_dump(mode="json")}, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        typer.echo(f"\nreport written to {report}")

    if not (result.all_passed and retrieval_ok):
        raise typer.Exit(code=1)


@app.command()
def manifest() -> None:
    """Print the component manifest for the current configuration."""
    with build_application() as application:
        _echo_json(
            {
                "configuration": application.describe(),
                "tools": describe_tools(ALL_TOOL_SPECS),
            }
        )


@app.command()
def serve(
    host: Annotated[str, typer.Option()] = "127.0.0.1",
    port: Annotated[int, typer.Option()] = 8000,
    reload: Annotated[bool, typer.Option(help="Reload on source change.")] = False,
) -> None:
    """Start the HTTP service.

    Binds to localhost by default. This service has no authentication, so binding it to a
    routable address would expose the approval endpoints to anyone who can reach the host.
    """
    import uvicorn

    uvicorn.run("ap_agent.api.app:app", host=host, port=port, reload=reload)


def main() -> None:  # pragma: no cover - console-script entry point
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":  # pragma: no cover
    main()
