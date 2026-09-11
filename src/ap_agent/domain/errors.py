"""Typed errors.

The distinction that matters operationally is transient versus permanent. A transient
failure is worth another attempt inside the budget; a permanent one is not, and retrying
it burns the tool budget that a later control needs. Tool backends therefore raise the
specific subclass, and the retry wrapper branches on the type rather than on a string
match against an error message.
"""

from __future__ import annotations


class APAgentError(Exception):
    """Base class for every error this system raises deliberately."""


class ConfigurationError(APAgentError):
    """Settings are missing or inconsistent. Not recoverable at runtime."""


# ---- tool failures ------------------------------------------------------------------


class ToolError(APAgentError):
    """Base class for tool failures, carrying the tool name for the event log."""

    def __init__(self, tool_name: str, message: str) -> None:
        super().__init__(f"{tool_name}: {message}")
        self.tool_name = tool_name
        self.message = message


class TransientToolError(ToolError):
    """A failure that a retry may resolve: upstream 5xx, connection reset, lock contention."""


class PermanentToolError(ToolError):
    """A failure a retry cannot resolve: unknown identifier, invalid argument, 4xx."""


class ToolTimeoutError(TransientToolError):
    """The tool did not answer inside its configured budget.

    Modelled as transient because a timeout carries no information about whether the
    upstream call succeeded. The consequence is that a timed-out *write* must be
    idempotent to be safe to retry, which is why ``submit_finance_decision`` keys on a
    hash computed before the first attempt.
    """


class ToolPermissionDenied(ToolError):
    """A write tool was called without an approved approval record.

    Raised by the tool itself, not only by the orchestrator, so that the guarantee does not
    depend on the caller being correct.
    """


# ---- model failures -----------------------------------------------------------------


class ModelError(APAgentError):
    """Base class for model adapter failures."""


class ModelOutputInvalid(ModelError):
    """The model produced output that failed schema validation after one repair attempt.

    Carries the validation error text for the event log. The raw output is deliberately not
    stored on the exception: it is redacted and logged once by the adapter, and passing it
    further would risk it reaching an unredacted surface.
    """

    def __init__(self, schema_name: str, validation_error: str) -> None:
        super().__init__(f"{schema_name} validation failed after repair: {validation_error}")
        self.schema_name = schema_name
        self.validation_error = validation_error


class ModelUnavailable(ModelError):
    """The provider could not be reached within the configured retries."""


# ---- orchestration failures ----------------------------------------------------------


class BudgetExhausted(APAgentError):
    """The run exceeded its step or tool-call budget.

    An explicit error rather than a silent stop: an unbounded loop is the failure mode this
    budget exists to prevent, so exhausting it must be visible in the audit trail.
    """

    def __init__(self, kind: str, limit: int) -> None:
        super().__init__(f"{kind} budget of {limit} exhausted")
        self.kind = kind
        self.limit = limit


class ApprovalStateConflict(APAgentError):
    """An approval callback arrived for a run that is not awaiting one, or the stored
    request hash for an idempotency key does not match the incoming request.

    The second case is deliberately an error, not a replay. Identical keys with different
    payloads mean a caller has reused a key for a different decision; returning the stored
    response would silently answer the wrong question.
    """


class RunNotFound(APAgentError):
    def __init__(self, run_id: str) -> None:
        super().__init__(f"run {run_id} not found")
        self.run_id = run_id


class IndexNotBuilt(APAgentError):
    """Retrieval was attempted before ingestion. Actionable message rather than a KeyError."""

    def __init__(self, index_dir: str) -> None:
        super().__init__(
            f"no retrieval index at {index_dir}. Run 'ap-agent ingest' first "
            "(see README, Ingestion and indexing)."
        )
