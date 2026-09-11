"""PreToolUse hook: block writes that would commit credentials or full bank details.

Reads the tool-call JSON from stdin. Exit code 2 blocks the tool call and feeds the
message back to the agent. Anything else lets the call proceed.

Why: the brief forbids API keys, credentials, personal data or bank details in the repo,
and FIN-POL-004 / FIN-POL-010 require bank numbers to be masked to the last four digits.
A hook enforces this structurally instead of relying on the author remembering.
"""
from __future__ import annotations

import json
import re
import sys

PATTERNS = {
    "anthropic_api_key": re.compile(r"sk-ant-[A-Za-z0-9\-_]{20,}"),
    "openai_style_key": re.compile(r"sk-[A-Za-z0-9]{32,}"),
    "aws_access_key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "private_key_block": re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "generic_secret_assignment": re.compile(
        r"(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*['\"][A-Za-z0-9\-_/+]{16,}['\"]"
    ),
    # Full BSB+account or IBAN-shaped strings. Last-four masking ("****8842") is allowed.
    "unmasked_bank_account": re.compile(r"\b\d{3}-?\d{3}\s?\d{6,10}\b|\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"),
}

ALLOWED_PATH_FRAGMENTS = (".env.example", "secret_guard.py", "docs/references.md")


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    tool_input = payload.get("tool_input", {}) or {}
    path = str(tool_input.get("file_path", ""))
    if any(fragment in path.replace("\\", "/") for fragment in ALLOWED_PATH_FRAGMENTS):
        return 0
    content = " ".join(
        str(tool_input.get(key, ""))
        for key in ("content", "new_string", "old_string")
        if tool_input.get(key)
    )
    if not content:
        return 0
    hits = [name for name, pattern in PATTERNS.items() if pattern.search(content)]
    if hits:
        sys.stderr.write(
            "secret_guard: refusing to write. Matched: "
            + ", ".join(hits)
            + ". Move secrets to environment variables and mask bank details to the last four digits.\n"
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
