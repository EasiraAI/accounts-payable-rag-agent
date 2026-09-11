"""PostToolUse hook: format and lint any Python file that was just written.

Runs ruff format + ruff check --fix on the touched file if ruff is installed. Never
blocks; it only reports. Keeps the codebase consistent without a separate pass.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    path = str((payload.get("tool_input") or {}).get("file_path", ""))
    if not path.endswith(".py") or "/.claude/" in path.replace("\\", "/"):
        return 0
    ruff = shutil.which("ruff")
    if not ruff:
        return 0
    subprocess.run([ruff, "format", path], check=False, capture_output=True)
    result = subprocess.run([ruff, "check", "--fix", path], check=False, capture_output=True, text=True)
    if result.returncode != 0:
        sys.stderr.write(result.stdout[-2000:])
    return 0


if __name__ == "__main__":
    sys.exit(main())
