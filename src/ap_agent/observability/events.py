"""Structured event emission.

This is the **single egress path** for run telemetry. Every event goes to two places from
here: the durable ``events`` table and the process log. Redaction is applied once, at this
boundary, so there is exactly one place to audit and exactly one place a mistake could be
made. No other module writes to the event table, and nothing else logs a tool or model
payload.

The brief requires timestamps, a run or correlation identifier, an outcome and a duration on
every event. Those are constructor arguments rather than payload keys, so an event cannot be
emitted without them.

Duration is measured with ``time.perf_counter``, a monotonic clock. Wall-clock deltas can go
backwards across an NTP adjustment and produce negative durations in an audit record.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ap_agent.domain.enums import EventType, RunPhase, ToolOutcome
from ap_agent.observability.redact import redact_payload
from ap_agent.persistence.repository import Repository

_logger = logging.getLogger("ap_agent.events")


@dataclass
class EventEmitter:
    """Emits redacted events for one run.

    Bound to a single run and correlation identifier at construction, so a caller cannot
    accidentally attribute an event to the wrong run.
    """

    repository: Repository
    run_id: str
    correlation_id: str
    #: Events emitted through this instance, in order. Kept in memory as well as persisted so
    #: that a test or an evaluation run can assert on them without a database read.
    emitted: list[dict[str, Any]] = field(default_factory=list)

    def emit(
        self,
        event_type: EventType,
        *,
        payload: dict[str, Any] | None = None,
        phase: RunPhase | None = None,
        outcome: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        """Record one event.

        The payload is redacted before it is written anywhere. Persistence failure is
        deliberately allowed to propagate: an audit trail that silently drops events is
        worse than a run that fails, because the resulting record looks complete.
        """
        safe_payload = redact_payload(payload or {})
        moment = datetime.now(tz=UTC)
        sequence = self.repository.append_event(
            run_id=self.run_id,
            event_type=event_type.value,
            correlation_id=self.correlation_id,
            payload=safe_payload,
            phase=phase.value if phase else None,
            outcome=outcome,
            duration_ms=duration_ms,
            created_at=moment,
        )
        record = {
            "sequence": sequence,
            "event_type": event_type.value,
            "run_id": self.run_id,
            "correlation_id": self.correlation_id,
            "phase": phase.value if phase else None,
            "outcome": outcome,
            "duration_ms": duration_ms,
            "timestamp": moment.isoformat(),
            "payload": safe_payload,
        }
        self.emitted.append(record)
        _logger.info("%s", record)

    @contextmanager
    def timed(
        self,
        event_type: EventType,
        *,
        phase: RunPhase | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Emit an event on exit, with its measured duration and an outcome.

        The yielded dictionary is mutable: the body adds detail as it learns it, and the
        event is emitted once on the way out. A failure inside the block still emits, with
        the outcome set from the exception, so a crashed phase leaves a record of how long it
        ran and why it stopped. Without that, the most interesting events would be the ones
        never written.
        """
        detail: dict[str, Any] = dict(payload or {})
        started = time.perf_counter()
        outcome = "SUCCESS"
        try:
            yield detail
        except Exception as error:
            outcome = type(error).__name__
            detail["error"] = str(error)
            raise
        finally:
            duration_ms = int((time.perf_counter() - started) * 1000)
            self.emit(
                event_type,
                payload=detail,
                phase=phase,
                outcome=detail.pop("outcome", outcome),
                duration_ms=duration_ms,
            )

    # ---- typed helpers ---------------------------------------------------------------
    #
    # Thin wrappers that fix the payload shape for the event kinds a reader compares across
    # runs. Free-form payloads drift; these do not.

    def tool_call(
        self,
        *,
        tool_name: str,
        outcome: ToolOutcome,
        duration_ms: int,
        attempt: int,
        attempts_allowed: int,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.emit(
            EventType.TOOL_CALL,
            payload={
                "tool": tool_name,
                "attempt": attempt,
                "attempts_allowed": attempts_allowed,
                **(detail or {}),
            },
            outcome=outcome.value,
            duration_ms=duration_ms,
        )

    def retrieval(
        self,
        *,
        query: str,
        query_terms: list[str],
        purpose: str,
        doc_types: list[str] | None,
        results: list[dict[str, Any]],
        duration_ms: int,
    ) -> None:
        """Record what was searched and what came back.

        The query terms are recorded alongside the natural-language query because the terms
        are what actually drove the ranking; an auditor reproducing a run needs them.
        """
        self.emit(
            EventType.RETRIEVAL,
            payload={
                "query": query,
                "query_terms": query_terms,
                "purpose": purpose,
                "doc_types": doc_types,
                "result_count": len(results),
                "results": results,
            },
            outcome="SUCCESS",
            duration_ms=duration_ms,
        )

    def model_call(
        self,
        *,
        provider: str,
        model: str,
        schema_name: str,
        outcome: str,
        duration_ms: int,
        attempt: int,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        validation_error: str | None = None,
    ) -> None:
        """Record a model call.

        FIN-POL-010 §4 requires provider, model and request identity to be auditable. The
        prompt and completion text are deliberately *not* recorded: they would duplicate
        untrusted supplier content into the audit trail, and the evidence that content was
        considered is already present as citations and indicators.
        """
        payload: dict[str, Any] = {
            "provider": provider,
            "model": model,
            "schema": schema_name,
            "attempt": attempt,
        }
        if input_tokens is not None:
            payload["input_tokens"] = input_tokens
        if output_tokens is not None:
            payload["output_tokens"] = output_tokens
        if validation_error:
            payload["validation_error"] = validation_error
        self.emit(EventType.MODEL_CALL, payload=payload, outcome=outcome, duration_ms=duration_ms)

    def injection_detected(
        self, *, source: str, pattern_codes: list[str], phase: RunPhase | None = None
    ) -> None:
        """Record an attempted instruction injection.

        Emitted as its own event type so that attempts are countable across runs without
        parsing payloads. The matched pattern codes are kept; the text is not, because the
        citation already points at it.
        """
        self.emit(
            EventType.INJECTION_ATTEMPT_DETECTED,
            payload={"source": source, "patterns": pattern_codes},
            phase=phase,
            outcome="BLOCKED",
        )


def configure_logging(*, level: str = "INFO", json_format: bool = True) -> None:
    """Configure process logging once.

    Idempotent: calling it twice does not add a second handler, which would double every
    line. The JSON formatter emits one object per line, which is what a log shipper expects
    and what makes the event stream queryable without a parser.
    """
    root = logging.getLogger("ap_agent")
    root.setLevel(level.upper())
    if root.handlers:
        return
    handler = logging.StreamHandler()
    if json_format:
        handler.setFormatter(
            logging.Formatter('{"level":"%(levelname)s","logger":"%(name)s","message":%(message)s}')
        )
    else:
        handler.setFormatter(logging.Formatter("%(levelname)-8s %(name)s %(message)s"))
    root.addHandler(handler)
    root.propagate = False
