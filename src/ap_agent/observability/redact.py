"""Redaction applied to every event payload and every log line.

FIN-POL-010 §2 requires that prompts and logs carry only the data necessary for the task,
with bank accounts, tax identifiers and personal contact details masked. FIN-POL-004 §2
narrows that further: general application logs may show only the last four digits of an
account.

This module is the single implementation of that rule. Nothing else in the codebase is
allowed to write a raw tool response to a log; ``tests/unit/test_redact.py`` asserts the
patterns, and a pre-write hook blocks source files that embed unmasked account numbers.

Design note: redaction is applied on the way *out* (at the event and log boundary) rather
than on the way in, so the orchestrator can still reason about full values in memory when
a control genuinely needs them. The trade-off is that every egress path must route through
``redact_payload``; there is exactly one such path, ``observability.events``.
"""

from __future__ import annotations

import re
from typing import Any, Final

MASK: Final = "[REDACTED]"

#: Field names whose values never appear in an event, whatever they contain.
SENSITIVE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "api_key",
        "anthropic_api_key",
        "authorization",
        "bank_account",
        "bank_account_number",
        "account_number",
        "iban",
        "bsb",
        "swift",
        "password",
        "secret",
        "token",
        "tax_id",
        "abn",
        "tfn",
        "contact_phone",
        "contact_email",
        "signature",
    }
)

#: Field names that are already masked by construction and must survive redaction, so an
#: auditor can still correlate a payment to an account without seeing the account.
ALLOWED_MASKED_KEYS: Final[frozenset[str]] = frozenset(
    {"account_last4", "masked_account_last4", "bank_account_last4"}
)

_PATTERNS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    # Provider API keys.
    (re.compile(r"sk-ant-[A-Za-z0-9\-_]{16,}"), MASK),
    (re.compile(r"\bsk-[A-Za-z0-9]{32,}\b"), MASK),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), MASK),
    # IBAN-shaped identifiers.
    (re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"), MASK),
    # Australian BSB + account, with or without separators.
    (re.compile(r"\b\d{3}-\d{3}\s?\d{6,10}\b"), MASK),
    # Bare account-like digit runs of 8 or more, keeping the last four for reconciliation.
    (re.compile(r"\b\d{4,}(\d{4})\b"), r"****\1"),
    # Australian Business Number, written with or without spaces.
    (re.compile(r"\b\d{2}\s?\d{3}\s?\d{3}\s?\d{3}\b"), MASK),
)

_MAX_TEXT_LENGTH: Final = 2_000


def redact_text(value: str) -> str:
    """Mask credentials and account identifiers inside a free-text value.

    Long values are truncated: an event log is an audit trail, not a document store, and
    an unbounded retrieved chunk would otherwise be duplicated into every event.
    """
    redacted = value
    for pattern, replacement in _PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    if len(redacted) > _MAX_TEXT_LENGTH:
        redacted = redacted[:_MAX_TEXT_LENGTH] + f"...[truncated {len(redacted)} chars]"
    return redacted


def redact_payload(payload: Any, *, _depth: int = 0) -> Any:
    """Recursively redact a JSON-shaped payload.

    Depth is bounded so that a cyclic or pathologically nested structure cannot stall the
    logging path. Reaching the limit is itself recorded in the output rather than silently
    dropping data.
    """
    if _depth > 12:
        return "[REDACTED: max depth]"
    if isinstance(payload, dict):
        result: dict[str, Any] = {}
        for key, value in payload.items():
            lowered = str(key).lower()
            if lowered in ALLOWED_MASKED_KEYS:
                result[key] = value
            elif lowered in SENSITIVE_KEYS or any(
                marker in lowered for marker in ("api_key", "secret", "password")
            ):
                result[key] = MASK
            else:
                result[key] = redact_payload(value, _depth=_depth + 1)
        return result
    if isinstance(payload, (list, tuple)):
        return [redact_payload(item, _depth=_depth + 1) for item in payload]
    if isinstance(payload, str):
        return redact_text(payload)
    return payload


def mask_account(account_number: str) -> str:
    """Render an account number as the last four digits only (FIN-POL-004 §2)."""
    digits = re.sub(r"\D", "", account_number)
    if len(digits) <= 4:
        return "****"
    return f"****{digits[-4:]}"
