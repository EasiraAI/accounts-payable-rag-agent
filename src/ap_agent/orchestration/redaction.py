"""Redacting untrusted case text before it is persisted.

Its own module because both the intake phase and the orchestrator need it, and importing it
from either would make this package import itself in a circle. That is a small thing, but it is
the reason the function is here rather than where it was written.
"""

from __future__ import annotations

from ap_agent.domain.request import ProcessingRequest
from ap_agent.observability.redact import redact_text


def redact_case_input(request: ProcessingRequest) -> ProcessingRequest:
    """Return the request with its untrusted free text redacted.

    Applied at intake, before the state is persisted. An earlier version redacted only at the
    event boundary, and a security review pasted a bank account, an IBAN, a provider key and
    a tax identifier into the case notes and read every one of them back out of
    ``GET /runs/{id}``: the run state was a second egress path that nothing covered.

    Both controls that read this text still work afterwards, which is why redacting here is
    safe rather than lossy. Injection detection reads imperative language, which redaction
    leaves untouched. The payment-instruction comparison reads a four-digit account tail,
    which redaction preserves and FIN-POL-004 §2 explicitly permits.
    """
    notes = request.notes
    redacted_notes = (
        notes.model_copy(update={"content": redact_text(notes.content)})
        if notes is not None
        else None
    )
    redacted_attachments = [
        attachment.model_copy(update={"text": redact_text(attachment.text)})
        for attachment in request.attachments
    ]
    return request.model_copy(update={"notes": redacted_notes, "attachments": redacted_attachments})
