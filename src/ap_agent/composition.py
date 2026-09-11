"""Composition root.

The single place where concrete implementations are chosen and wired. The API, the CLI and
the evaluation runner all build their dependencies here, so there is one answer to "what is
this system actually made of" and no chance of the transports diverging: a change to how the
retriever is configured cannot apply to the CLI and miss the HTTP surface.

Everything below the transports receives its dependencies as constructor arguments and names
no implementation of its own. That is what lets a test substitute a fake model adapter, a
temporary database and a fixed clock without patching anything.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ap_agent.config.settings import Settings, get_settings
from ap_agent.domain.run_state import utc_now
from ap_agent.llm import build_llm_client
from ap_agent.llm.base import LLMClient
from ap_agent.observability.events import configure_logging
from ap_agent.orchestration.machine import Orchestrator
from ap_agent.persistence.repository import Repository
from ap_agent.rag.index import CorpusIndex, load_or_build_index
from ap_agent.rag.retriever import Retriever


@dataclass
class Application:
    """The assembled system.

    Holds the long-lived collaborators so a transport can serve many requests without
    rebuilding the index or reopening the database on each one.
    """

    settings: Settings
    repository: Repository
    index: CorpusIndex
    retriever: Retriever
    llm_client: LLMClient
    orchestrator: Orchestrator

    def close(self) -> None:
        self.repository.close()

    def __enter__(self) -> Application:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def describe(self) -> dict[str, object]:
        """What this instance is made of. Used by the manifest and the CLI status output."""
        return {
            "provider": self.llm_client.provider_name,
            "model": self.llm_client.model_name,
            "retrieval_mode": self.settings.retrieval_mode,
            "corpus_documents": self.index.document_count,
            "corpus_chunks": len(self.index),
            "corpus_hash": self.index.corpus_hash[:16],
            "superseded_score_factor": self.settings.superseded_score_factor,
            "database": str(self.settings.db_path),
            # The shape of the store that is actually open, not the constant this build
            # expects. They agree after a successful open, and an operator asserting on the
            # value wants the one the database reports.
            "schema_version": self.repository.schema_version,
            "index_dir": str(self.settings.index_dir),
            "max_steps": self.settings.max_steps,
            "max_tool_calls": self.settings.max_tool_calls,
            "tool_timeout_seconds": self.settings.tool_timeout_seconds,
            "tool_max_retries": self.settings.tool_max_retries,
        }


def build_application(
    *,
    settings: Settings | None = None,
    llm_client: LLMClient | None = None,
    clock: Callable[[], datetime] | None = None,
    mock_data_dir: Path | None = None,
    configure_logs: bool = True,
) -> Application:
    """Assemble the system from configuration.

    The index is built on first use if it is absent or stale, so a fresh checkout works with
    one command rather than failing on a missing index. Ingestion is idempotent: an unchanged
    corpus produces the same hash and the existing index is reused.
    """
    resolved = settings or get_settings()
    if configure_logs:
        configure_logging(level=resolved.log_level, json_format=resolved.log_format == "json")

    repository = Repository(resolved.db_path)
    index = load_or_build_index(resolved.corpus_dir, resolved.index_dir)
    retriever = Retriever(
        index,
        superseded_score_factor=resolved.superseded_score_factor,
        default_top_k=resolved.retrieval_top_k,
    )
    client = llm_client or build_llm_client(resolved)
    orchestrator = Orchestrator(
        settings=resolved,
        repository=repository,
        retriever=retriever,
        llm_client=client,
        clock=clock or utc_now,
        mock_data_dir=mock_data_dir,
    )
    return Application(
        settings=resolved,
        repository=repository,
        index=index,
        retriever=retriever,
        llm_client=client,
        orchestrator=orchestrator,
    )
