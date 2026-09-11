"""Model adapter contract.

One method, ``complete_structured``, which takes a system prompt, a user prompt and a
pydantic model, and returns an instance of that model or raises. There is no method that
returns free text, so no caller can accidentally consume unvalidated output.

## The repair attempt, and why there is only one

When validation fails the adapter retries once, including the validation error in the
prompt. A second failure raises ``ModelOutputInvalid`` and the run fails explicitly.

One repair rather than a loop, for two reasons. Empirically the first repair either works or
the model is confused about the schema in a way further attempts do not fix. And a retry loop
around a model call is the unbounded-loop failure mode the brief asks to avoid; a fixed
budget makes the worst case knowable.

There is deliberately no fallback to free-text parsing. A regular expression over prose that
failed to validate is how silently wrong evidence enters a financial control.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from ap_agent.domain.errors import ModelOutputInvalid, ModelUnavailable

__all__ = ["LLMClient", "ModelCallRecord", "ModelOutputInvalid", "ModelUnavailable"]


class ModelCallRecord(BaseModel):
    """What a completed model call reports about itself, for the audit trail.

    Token counts are optional because not every adapter can supply them. The fake adapter
    reports zero rather than omitting them, so a cost report has a uniform shape across
    tiers.
    """

    provider: str
    model: str
    schema_name: str
    attempts: int
    input_tokens: int = 0
    output_tokens: int = 0
    repaired: bool = False


@runtime_checkable
class LLMClient(Protocol):
    """Anything that can turn a prompt into a validated pydantic model.

    A ``Protocol`` rather than an abstract base class: the adapters share no implementation,
    and structural typing means a test double needs no import from this module to satisfy
    the contract.
    """

    @property
    def provider_name(self) -> str: ...

    @property
    def model_name(self) -> str: ...

    def complete_structured[T: BaseModel](
        self,
        *,
        system: str,
        user: str,
        schema: type[T],
        max_tokens: int | None = None,
    ) -> tuple[T, ModelCallRecord]:
        """Return a validated instance of ``schema``.

        Raises ``ModelOutputInvalid`` if validation fails after one repair attempt, and
        ``ModelUnavailable`` if the provider could not be reached within its retry budget.
        """
        ...


REPAIR_INSTRUCTION = """\
Your previous response did not match the required schema and was rejected.

Validation error:
{error}

Return a response that satisfies the schema exactly. Do not explain the correction, do not \
wrap the object in prose, and do not add fields that are not in the schema.
"""
