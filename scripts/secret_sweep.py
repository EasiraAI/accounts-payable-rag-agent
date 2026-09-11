"""Fail if the repository contains a credential-shaped or unmasked-account-shaped string.

The brief forbids API keys, credentials, personal data and proprietary code in the repository,
and FIN-POL-004 and FIN-POL-010 require bank details to be masked to the last four digits.
Both are easy to satisfy on the day and easy to lose later, so the claim is a script rather
than a sentence in the README: run it and find out.

    uv run python scripts/secret_sweep.py

Exits 0 when the tree is clean and 1 with a report otherwise, so it works as a pipeline step.

The patterns deliberately allow last-four masking ("****8842"), because that is the form the
policy requires and the vendor tool returns. Test inputs that need to *look* like a credential
are assembled at runtime from fragments, so this script has nothing to flag and the repository
contains no credential-shaped literal.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[1]

PATTERNS: Final[dict[str, re.Pattern[str]]] = {
    "anthropic_api_key": re.compile(r"sk-ant-[A-Za-z0-9\-_]{20,}"),
    "openai_style_key": re.compile(r"sk-[A-Za-z0-9]{32,}"),
    "aws_access_key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "private_key_block": re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "generic_secret_assignment": re.compile(
        r"(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*['\"][A-Za-z0-9\-_/+]{16,}['\"]"
    ),
    # A full BSB-and-account or IBAN-shaped string. Last-four masking is allowed.
    "unmasked_bank_account": re.compile(
        r"\b\d{3}-?\d{3}\s?\d{6,10}\b|\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"
    ),
}

#: Directories that hold generated or third-party content rather than committed source.
SKIP_DIRECTORIES: Final = frozenset(
    {
        ".venv",
        ".git",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "data",
        "node_modules",
    }
)

#: Paths whose whole purpose is to describe the shape of a credential. Each is listed
#: individually rather than by directory, so adding an exemption is a visible decision.
ALLOWED_PATHS: Final = frozenset(
    {
        ".env.example",
        "scripts/secret_sweep.py",
        "docs/references.md",
    }
)

#: File suffixes worth reading. A binary match would be a false positive, and the repository
#: holds no binary assets.
TEXT_SUFFIXES: Final = frozenset(
    {".py", ".md", ".json", ".sql", ".toml", ".yaml", ".yml", ".sh", ".ps1", ".txt", ".cfg"}
)


def findings(root: Path) -> list[tuple[str, int, str]]:
    """Every match in the tree, as (path, line number, pattern name)."""
    found: list[tuple[str, int, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in TEXT_SUFFIXES:
            continue
        if any(part in SKIP_DIRECTORIES for part in path.parts):
            continue
        relative = path.relative_to(root).as_posix()
        if relative in ALLOWED_PATHS:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for name, pattern in PATTERNS.items():
            for match in pattern.finditer(text):
                found.append((relative, text[: match.start()].count("\n") + 1, name))
    return found


def main() -> int:
    hits = findings(REPO_ROOT)
    if not hits:
        print(f"secret sweep: clean, {len(PATTERNS)} patterns, no findings")
        return 0
    print(f"secret sweep: {len(hits)} finding(s)")
    for relative, line, name in hits:
        # The matched text is deliberately not printed. Echoing a credential into a build log
        # is the same disclosure as committing it.
        print(f"  {relative}:{line}  {name}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
