"""How a provider failure is reported.

These exist because of a diagnostic dead end. The first live run of the Anthropic path failed
all five fixture cases with:

    INTERNAL_ERROR: provider returned 400 after 2 retries

which reads like a malformed request, and sent me into the request construction looking for the
bug. There was no bug. The provider had said:

    Your credit balance is too low to access the Anthropic API.

An account condition a person fixes in a minute, reported as an internal error, with a retry
history that never happened: the SDK does not retry a 400. Discarding the provider's message
cost more time than any other defect in this project, which is why the reporting now has tests
of its own.
"""

from __future__ import annotations

from typing import Any

from ap_agent.llm.anthropic_client import _provider_message, _status_error_detail


class _StatusError:
    """A stand-in for the SDK's APIStatusError, carrying only what the reporting reads.

    A stub rather than the real class so these tests do not need the provider SDK installed,
    and so a change in the SDK's constructor cannot silently disable them.
    """

    def __init__(self, status_code: int, body: Any = None, message: str = "") -> None:
        self.status_code = status_code
        self.body = body
        self.message = message


def _billing_body(message: str = "Your credit balance is too low.") -> dict[str, Any]:
    return {"type": "error", "error": {"type": "invalid_request_error", "message": message}}


class TestProviderMessage:
    def test_the_message_is_read_from_the_error_body(self) -> None:
        error = _StatusError(400, body=_billing_body())
        assert _provider_message(error) == "Your credit balance is too low."

    def test_a_plain_message_attribute_is_used_when_there_is_no_body(self) -> None:
        assert _provider_message(_StatusError(500, message="upstream exploded")) == (
            "upstream exploded"
        )

    def test_an_unrecognised_shape_yields_nothing_rather_than_a_repr(self) -> None:
        """A stringified object in an audit log is noise that looks like information."""
        assert _provider_message(_StatusError(400, body=["unexpected"])) == ""

    def test_an_error_with_no_detail_at_all_yields_nothing(self) -> None:
        assert _provider_message(object()) == ""


class TestStatusErrorDetail:
    def test_a_400_is_named_permanent_and_not_described_as_retried(self) -> None:
        detail = _status_error_detail(_StatusError(400, body=_billing_body()), max_retries=2)
        assert "not retried" in detail
        assert "permanent" in detail
        assert "retries" not in detail.replace("not retried", "")

    def test_the_provider_explanation_survives_into_the_detail(self) -> None:
        """The whole point: the reader learns what to do about it."""
        detail = _status_error_detail(
            _StatusError(400, body=_billing_body("Your credit balance is too low.")),
            max_retries=2,
        )
        assert "credit balance" in detail

    def test_a_429_is_transient_and_reports_the_retry_budget(self) -> None:
        detail = _status_error_detail(_StatusError(429), max_retries=2)
        assert "after 2 retries" in detail

    def test_a_500_is_transient(self) -> None:
        assert "after 2 retries" in _status_error_detail(_StatusError(503), max_retries=2)

    def test_a_408_and_409_are_transient_because_the_sdk_retries_them(self) -> None:
        for status in (408, 409):
            assert "after 2 retries" in _status_error_detail(_StatusError(status), max_retries=2)

    def test_a_401_is_permanent_because_a_second_attempt_uses_the_same_key(self) -> None:
        detail = _status_error_detail(_StatusError(401), max_retries=2)
        assert "not retried" in detail

    def test_the_retry_count_is_singular_when_there_is_one(self) -> None:
        assert "after 1 retry" in _status_error_detail(_StatusError(429), max_retries=1)

    def test_the_status_is_always_reported(self) -> None:
        assert "404" in _status_error_detail(_StatusError(404), max_retries=2)
