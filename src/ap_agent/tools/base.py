"""Tool execution contract.

Every tool in this system declares four things up front, and the runner enforces them:

``permission``  READ or WRITE. There is no third level and no argument that widens it.
``timeout``     A wall-clock ceiling. A tool that does not answer is a failure, not a wait.
``retries``     A finite count. There is no unbounded retry anywhere in the system.
``schemas``     Typed input and output. Arguments are validated before the call and results
                after it, so a malformed response is a tool failure rather than corrupt
                evidence flowing into a control.

## Why the timeout uses a thread

``concurrent.futures.ThreadPoolExecutor`` with a ``result(timeout=...)`` is used rather than
a signal-based alarm. Signals only work on the main thread and only on POSIX, and this
service runs on Windows as well. The honest limitation of the thread approach is that a
timed-out call is *abandoned*, not cancelled: the worker thread may still be running. That is
acceptable here because every tool with a timeout is a read. It would not be acceptable for
the write tool, which is why ``submit_finance_decision`` is idempotent: if it were abandoned
mid-flight and retried, the key collision makes the second attempt a no-op.

## Why a timeout is classified as transient

A timeout carries no information about whether the far side completed. Treating it as
retryable is the only safe default for a read. For a write it would be dangerous without
idempotency, which is the reason the write tool has it.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from ap_agent.domain.enums import ToolOutcome, ToolPermission
from ap_agent.domain.errors import (
    PermanentToolError,
    ToolError,
    ToolTimeoutError,
    TransientToolError,
)
from ap_agent.observability.events import EventEmitter


@dataclass(frozen=True)
class ToolSpec:
    """The declared contract of a tool.

    ``purpose`` is not decoration: it is what a reviewer reads to decide whether the
    permission level is justified. A READ tool whose purpose describes a write is a finding.
    """

    name: str
    purpose: str
    permission: ToolPermission
    timeout_seconds: float
    max_retries: int
    #: Backoff base in seconds. Attempt *n* waits ``backoff_seconds * 2 ** (n - 1)``.
    backoff_seconds: float = 0.05

    @property
    def attempts_allowed(self) -> int:
        """Total attempts including the first. A retry count of 2 means 3 attempts."""
        return self.max_retries + 1


class ToolCallResult[OutputT: BaseModel](BaseModel):
    """The outcome of one tool invocation, successful or not.

    Returned rather than raised for the read tools. A run must be able to continue with a
    recorded gap in its evidence: FIN-POL-001 §5 requires missing evidence to produce a
    hold, and it cannot produce a hold if the missing evidence crashed the run. The write
    tool is the exception and raises, because a failure to record a decision must not be
    mistaken for a decision.
    """

    model_config = {"arbitrary_types_allowed": True}

    tool_name: str
    outcome: ToolOutcome
    attempts: int
    duration_ms: int
    value: OutputT | None = None
    error_type: str | None = None
    error_message: str = ""

    @property
    def succeeded(self) -> bool:
        return self.outcome is ToolOutcome.SUCCESS and self.value is not None


class ToolRunner:
    """Executes tools under their declared timeout, retry and budget constraints."""

    def __init__(
        self,
        *,
        emitter: EventEmitter,
        max_tool_calls: int,
        calls_already_used: int = 0,
    ) -> None:
        """``calls_already_used`` carries a resumed run's spent budget forward.

        Without it a resumed run would start from zero and could spend the full budget
        again on every restart, which would make the bound meaningless for exactly the
        runs most likely to need one.
        """
        self._emitter = emitter
        self._max_tool_calls = max_tool_calls
        self._calls_used = calls_already_used

    @property
    def calls_used(self) -> int:
        """Attempts consumed, not distinct tools.

        A retry spends budget. Counting only distinct tools would let a flapping dependency
        consume unbounded wall-clock time inside a nominally bounded run.
        """
        return self._calls_used

    @property
    def budget_remaining(self) -> int:
        return max(0, self._max_tool_calls - self._calls_used)

    def _call_once[InputT: BaseModel, OutputT: BaseModel](
        self,
        spec: ToolSpec,
        handler: Callable[[InputT], OutputT],
        arguments: InputT,
    ) -> OutputT:
        """Invoke the handler with a hard timeout."""
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"tool-{spec.name}") as pool:
            future = pool.submit(handler, arguments)
            try:
                return future.result(timeout=spec.timeout_seconds)
            except FutureTimeout as error:
                # The worker is abandoned rather than cancelled; see the module docstring.
                future.cancel()
                raise ToolTimeoutError(
                    spec.name, f"no response within {spec.timeout_seconds}s"
                ) from error

    def run[InputT: BaseModel, OutputT: BaseModel](
        self,
        spec: ToolSpec,
        handler: Callable[[InputT], OutputT],
        arguments: InputT,
        output_model: type[OutputT],
    ) -> ToolCallResult[OutputT]:
        """Execute a read tool, returning a result rather than raising.

        Retries only transient failures. A permanent failure is not retried: an unknown
        vendor identifier will still be unknown on the third attempt, and spending the budget
        on it starves a later control that could have succeeded.
        """
        started = time.perf_counter()
        last_error: ToolError | None = None
        attempt = 0

        while attempt < spec.attempts_allowed:
            if self.budget_remaining == 0:
                duration_ms = int((time.perf_counter() - started) * 1000)
                self._emitter.tool_call(
                    tool_name=spec.name,
                    outcome=ToolOutcome.DENIED,
                    duration_ms=duration_ms,
                    attempt=attempt + 1,
                    attempts_allowed=spec.attempts_allowed,
                    detail={"reason": "tool-call budget exhausted"},
                )
                return ToolCallResult[output_model](  # type: ignore[valid-type]
                    tool_name=spec.name,
                    outcome=ToolOutcome.DENIED,
                    attempts=attempt,
                    duration_ms=duration_ms,
                    error_type="BudgetExhausted",
                    error_message=f"tool-call budget of {self._max_tool_calls} exhausted",
                )

            attempt += 1
            self._calls_used += 1
            attempt_started = time.perf_counter()
            try:
                raw = self._call_once(spec, handler, arguments)
                value = output_model.model_validate(raw, from_attributes=True)
            except ValidationError as error:
                # A tool whose response does not match its declared schema is broken, and
                # retrying will produce the same shape. Treated as permanent.
                last_error = PermanentToolError(spec.name, f"response failed validation: {error}")
                self._emitter.tool_call(
                    tool_name=spec.name,
                    outcome=ToolOutcome.PERMANENT_ERROR,
                    duration_ms=int((time.perf_counter() - attempt_started) * 1000),
                    attempt=attempt,
                    attempts_allowed=spec.attempts_allowed,
                    detail={"error": "schema validation failed"},
                )
                break
            except ToolTimeoutError as error:
                last_error = error
                self._emitter.tool_call(
                    tool_name=spec.name,
                    outcome=ToolOutcome.TIMEOUT,
                    duration_ms=int((time.perf_counter() - attempt_started) * 1000),
                    attempt=attempt,
                    attempts_allowed=spec.attempts_allowed,
                    detail={"timeout_seconds": spec.timeout_seconds},
                )
            except TransientToolError as error:
                last_error = error
                self._emitter.tool_call(
                    tool_name=spec.name,
                    outcome=ToolOutcome.TRANSIENT_ERROR,
                    duration_ms=int((time.perf_counter() - attempt_started) * 1000),
                    attempt=attempt,
                    attempts_allowed=spec.attempts_allowed,
                    detail={"error": error.message},
                )
            except PermanentToolError as error:
                last_error = error
                self._emitter.tool_call(
                    tool_name=spec.name,
                    outcome=ToolOutcome.PERMANENT_ERROR,
                    duration_ms=int((time.perf_counter() - attempt_started) * 1000),
                    attempt=attempt,
                    attempts_allowed=spec.attempts_allowed,
                    detail={"error": error.message},
                )
                break
            else:
                duration_ms = int((time.perf_counter() - attempt_started) * 1000)
                self._emitter.tool_call(
                    tool_name=spec.name,
                    outcome=ToolOutcome.SUCCESS,
                    duration_ms=duration_ms,
                    attempt=attempt,
                    attempts_allowed=spec.attempts_allowed,
                )
                return ToolCallResult[output_model](  # type: ignore[valid-type]
                    tool_name=spec.name,
                    outcome=ToolOutcome.SUCCESS,
                    attempts=attempt,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                    value=value,
                )

            if attempt < spec.attempts_allowed and isinstance(last_error, TransientToolError):
                time.sleep(spec.backoff_seconds * (2 ** (attempt - 1)))

        outcome = (
            ToolOutcome.TIMEOUT
            if isinstance(last_error, ToolTimeoutError)
            else ToolOutcome.TRANSIENT_ERROR
            if isinstance(last_error, TransientToolError)
            else ToolOutcome.PERMANENT_ERROR
        )
        return ToolCallResult[output_model](  # type: ignore[valid-type]
            tool_name=spec.name,
            outcome=outcome,
            attempts=attempt,
            duration_ms=int((time.perf_counter() - started) * 1000),
            error_type=type(last_error).__name__ if last_error else None,
            error_message=last_error.message if last_error else "",
        )


def describe_tools(specs: list[ToolSpec]) -> list[dict[str, Any]]:
    """Render the tool contracts for documentation and the manifest.

    Generated from the specs rather than written by hand, so the manifest cannot drift from
    the code it describes.
    """
    return [
        {
            "name": spec.name,
            "purpose": spec.purpose,
            "permission": spec.permission.value,
            "timeout_seconds": spec.timeout_seconds,
            "max_retries": spec.max_retries,
            "attempts_allowed": spec.attempts_allowed,
        }
        for spec in specs
    ]
