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
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ValidationError

from ap_agent.config.settings import Settings
from ap_agent.domain.errors import ModelOutputInvalid, ModelUnavailable
from ap_agent.llm.base import REPAIR_INSTRUCTION, ModelCallRecord

#: Name of the single forced tool. The model sees this, so it reads as an instruction.
_RESPONSE_TOOL_NAME = "report_analysis"


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

        try:
            response = self._client.messages.create(
                model=self._settings.llm_model,
                max_tokens=max_tokens,
                system=system,
                tools=[self._tool_definition(schema)],
                tool_choice={"type": "tool", "name": _RESPONSE_TOOL_NAME},
                messages=[{"role": "user", "content": user}],
            )
        except APIStatusError as error:
            raise ModelUnavailable(
                f"provider returned {error.status_code} after "
                f"{self._settings.llm_max_retries} retries"
            ) from error
        except APIError as error:
            raise ModelUnavailable(f"provider call failed: {error}") from error

        for block in response.content:
            if getattr(block, "type", None) == "tool_use" and block.name == _RESPONSE_TOOL_NAME:
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
