"""What a phase is allowed to reach.

Every phase used to be a method on the orchestrator, which meant every phase could reach
everything the orchestrator holds: the store, the model, the budget, the tool runner, the
retriever, the settings, and thirty private helpers. Nothing enforced that the intake phase
never touched the decision table, and nothing told a reader that it does not.

This protocol is four members wide. A phase that only receives a context can only reach a
clock, a repository, a retriever and a model call, so what it is capable of is legible from its
signature rather than from reading its body. That is the same argument the rest of this system
makes about tools: bound the permission, then the reader does not have to trust the
implementation.

It is a ``Protocol`` rather than a base class or a dataclass for one reason. The orchestrator
satisfies it structurally, by exposing four public members, so there is no wrapper object to
construct, nothing to keep in sync, and no second source of truth about the run. A test can
pass any object with these four members, which is what makes a phase testable without building
an orchestrator at all.

Deliberately absent: the settings, the budget and the tool runner. A phase receives its runner
as an argument, because the runner carries the budget already spent and handing it out through
a context would make it ambient. Settings are absent because a phase that needs a threshold
should receive the value, not the configuration.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # pragma: no cover - typing only
    from datetime import datetime

    from pydantic import BaseModel

    from ap_agent.domain.run_state import RunState
    from ap_agent.observability.events import EventEmitter
    from ap_agent.persistence.repository import Repository
    from ap_agent.rag.retriever import Retriever


class PhaseContext(Protocol):
    """The collaborators a phase handler may use."""

    @property
    def clock(self) -> Callable[[], datetime]:
        """The run's clock.

        Injected rather than read from ``datetime.now`` so that a fixture run is reproducible
        and an evaluation can pin the date every policy window is measured against.
        """
        ...

    @property
    def repository(self) -> Repository:
        """The durable store. Phases read run state and approvals; only the decision phase
        writes through the approval-gated tool."""
        ...

    @property
    def retriever(self) -> Retriever:
        """Policy and evidence retrieval, already configured with its ranking mode."""
        ...

    def call_model[T: BaseModel](
        self,
        state: RunState,
        emitter: EventEmitter,
        prompt: str,
        schema: type[T],
    ) -> T:
        """One structured model call, validated, with one repair attempt and then failure.

        A method rather than an exposed client, so a phase cannot reach past the validation,
        the retry budget or the event record. The two phases that call a model get prose; the
        outcome is computed before either of them runs.
        """
        ...


if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable
