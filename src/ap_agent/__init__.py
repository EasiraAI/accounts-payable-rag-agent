"""Accounts-payable processing and RAG workflow agent.

Layering, innermost first. Each layer may import only from layers above it in this list:

    config          environment-driven settings; no imports from the rest of the package
    domain          typed contracts and pure rule functions; no I/O
    observability   redaction and event emission
    persistence     SQLite repository
    rag             ingestion, index, retrieval
    llm             provider adapters and prompt construction
    tools           the five tool contracts and their backends
    orchestration   the state machine that drives a run
    api / cli       transport

The dependency direction is enforced by review, not by tooling. A lower layer importing
a higher one is a design error: it means policy logic has leaked into transport.
"""

__all__ = ["__version__"]

__version__ = "1.0.0"
