"""Live model adapter.

Labelled in the README as requiring external access. Nothing in the default test or
evaluation tier calls it.

## Structured output via forced tool use

The response is obtained by declaring a single tool whose input schema is the pydantic
model's JSON schema, and setting ``tool_choice`` to that tool. The model must answer by
calling it, so the response arrives as a JSON object shaped by the schema rather than as
prose that has to be parsed out of a reply.

The alternative, asking for JSON in the prompt and extracting it, fails in ways that matter
here: fenced code blocks, preambles, trailing commentary, and a model that explains itself
instead of answering. Each of those needs a heuristic, and a heuristic in front of a
financial control is a defect waiting for an unusual input.

Validation still runs on the result. Forced tool use constrains the shape, not the semantics:
the schema cannot express "confidence must be between 0 and 1 *and* its basis must be
non-empty", and pydantic can.

## Retries

Two retry budgets, deliberately separate. Transport failures are retried by the SDK, which
is configured with ``max_retries`` from settings. Schema failures are retried once here, by
re-prompting with the validation error. Conflating them would let a validation problem
consume the transport budget and produce a misleading "provider unavailable".

## Reporting a provider failure

The provider's own message is included in the raised error, and a permanent status is named as
permanent. That sounds obvious; it was not what this adapter did.

The first live run of this code failed all five cases with
``INTERNAL_ERROR: provider returned 400 after 2 retries``. That message cost real diagnostic
time, because it pointed at the request construction. The request was correct. What the
provider had actually said was "Your credit balance is too low to access the Anthropic API" —
an account condition a person fixes in a minute, reported as an internal error with an
invented retry history. The SDK does not retry a 400 at all.

So the distinction is now drawn where it belongs: a 408, 409, 429 or 5xx is transient and was
retried; every other 4xx is permanent, was not retried, and says so. Provider error messages
are not sensitive and are recorded; the API key never appears in one.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Final, cast

if TYPE_CHECKING:  # pragma: no cover - imported for typing only
    from anthropic.types import MessageParam, ToolChoiceToolParam, ToolParam

from pydantic import BaseModel, ValidationError

from ap_agent.config.settings import Settings
from ap_agent.domain.errors import ModelOutputInvalid, ModelUnavailable
from ap_agent.llm.base import REPAIR_INSTRUCTION, ModelCallRecord

#: Name of the single forced tool. The model sees this, so it reads as an instruction.
_RESPONSE_TOOL_NAME = "report_analysis"


#: Statuses the SDK retries. Everything else in the 4xx range is a permanent client or account
#: condition: retrying a malformed request, an invalid key or an exhausted balance produces the
#: same answer and delays the report to whoever can fix it.
_TRANSIENT_STATUSES: Final = frozenset({408, 409, 429})


def _provider_message(error: object) -> str:
    """The provider's own explanation, or an empty string.

    Dug out of the error body rather than ``str(error)`` because the SDK's string form leads
    with the status line and truncates awkwardly. Nothing here can carry a credential: the body
    is the provider's response, and the key travels in a request header.
    """
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        inner = body.get("error")
        if isinstance(inner, dict):
            message = inner.get("message")
            if isinstance(message, str):
                return message
    message = getattr(error, "message", None)
    return message if isinstance(message, str) else ""


def _status_error_detail(error: object, max_retries: int) -> str:
    """Describe a provider status failure accurately, including whether it was retried."""
    status = getattr(error, "status_code", None)
    transient = isinstance(status, int) and (status in _TRANSIENT_STATUSES or status >= 500)
    attempts = (
        f"after {max_retries} retr{'y' if max_retries == 1 else 'ies'}"
        if transient
        else "not retried: a permanent client or account condition"
    )
    message = _provider_message(error)
    detail = f"provider returned {status} ({attempts})"
    return f"{detail}: {message}" if message else detail


class AnthropicClient:
    """Adapter for Claude, using forced tool use to obtain schema-shaped output."""

    def __init__(self, settings: Settings) -> None:
        try:
            from anthropic import Anthropic
        except ImportError as error:  # pragma: no cover - dependency is declared
            raise ModelUnavailable(
                "the anthropic package is not installed; install it or set AP_LLM_PROVIDER=fake"
            ) from error

        self._settings = settings
        self._client = Anthropic(
            api_key=settings.require_api_key(),
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )

    @property
    def provider_name(self) -> str:
        return "anthropic"

    @property
    def model_name(self) -> str:
        return self._settings.llm_model

    @staticmethod
    def _tool_definition(schema: type[BaseModel]) -> dict[str, Any]:
        """Declare the response schema as a tool the model must call."""
        json_schema = schema.model_json_schema()
        return {
            "name": _RESPONSE_TOOL_NAME,
            "description": (
                f"Report your analysis as a {schema.__name__} object. Every field must "
                "satisfy the schema. This is the only way to respond."
            ),
            "input_schema": json_schema,
        }

    def _call(
        self, *, system: str, user: str, schema: type[BaseModel], max_tokens: int
    ) -> tuple[dict[str, Any], int, int]:
        """One request. Returns the tool input plus token counts."""
        from anthropic import APIError, APIStatusError
        from anthropic.types import ToolUseBlock

        try:
            # The tool definition is built from a pydantic JSON schema at runtime, so it
            # cannot be expressed as the SDK's TypedDict literal. Cast at the boundary and
            # keep the construction in one place, rather than loosening the adapter's own
            # type discipline.
            tool_param = cast("ToolParam", self._tool_definition(schema))
            tool_choice = cast("ToolChoiceToolParam", {"type": "tool", "name": _RESPONSE_TOOL_NAME})
            messages = [cast("MessageParam", {"role": "user", "content": user})]
            response = self._client.messages.create(
                model=self._settings.llm_model,
                max_tokens=max_tokens,
                system=system,
                tools=[tool_param],
                tool_choice=tool_choice,
                messages=messages,
            )
        except APIStatusError as error:
            raise ModelUnavailable(
                _status_error_detail(error, self._settings.llm_max_retries)
            ) from error
        except APIError as error:
            raise ModelUnavailable(f"provider call failed: {error}") from error

        # Narrowed by isinstance rather than by inspecting a "type" string. A response may
        # carry any of a dozen block kinds, and only ToolUseBlock has the name and input
        # attributes read below; checking the class means a future block kind cannot be
        # mistaken for a tool call.
        for block in response.content:
            if not isinstance(block, ToolUseBlock) or block.name != _RESPONSE_TOOL_NAME:
                continue
            payload = block.input
            if not isinstance(payload, dict):
                raise ModelOutputInvalid(
                    schema.__name__, f"tool input was {type(payload).__name__}, not an object"
                )
            return (
                payload,
                response.usage.input_tokens,
                response.usage.output_tokens,
            )

        # Forced tool use should make this unreachable. Raising rather than falling back to
        # text parsing keeps the "no free-text path" guarantee true even if the provider's
        # behaviour changes.
        raise ModelOutputInvalid(
            schema.__name__,
            "response contained no tool_use block despite tool_choice forcing one",
        )

    def complete_structured[T: BaseModel](
        self,
        *,
        system: str,
        user: str,
        schema: type[T],
        max_tokens: int | None = None,
    ) -> tuple[T, ModelCallRecord]:
        budget = max_tokens or self._settings.llm_max_tokens
        payload, input_tokens, output_tokens = self._call(
            system=system, user=user, schema=schema, max_tokens=budget
        )

        try:
            value = schema.model_validate(payload)
        except ValidationError as first_error:
            repair_prompt = (
                user
                + "\n\n"
                + REPAIR_INSTRUCTION.format(error=str(first_error))
                + "\n\nYour rejected response was:\n"
                + json.dumps(payload, default=str)[:4_000]
            )
            repaired_payload, repair_in, repair_out = self._call(
                system=system, user=repair_prompt, schema=schema, max_tokens=budget
            )
            try:
                value = schema.model_validate(repaired_payload)
            except ValidationError as second_error:
                raise ModelOutputInvalid(schema.__name__, str(second_error)) from second_error
            return value, ModelCallRecord(
                provider=self.provider_name,
                model=self.model_name,
                schema_name=schema.__name__,
                attempts=2,
                input_tokens=input_tokens + repair_in,
                output_tokens=output_tokens + repair_out,
                repaired=True,
            )

        return value, ModelCallRecord(
            provider=self.provider_name,
            model=self.model_name,
            schema_name=schema.__name__,
            attempts=1,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            repaired=False,
        )
