#!/usr/bin/env bash
# One-command local setup. Idempotent: safe to re-run.
set -euo pipefail
cd "$(dirname "$0")/.."

command -v uv >/dev/null 2>&1 || { echo "uv is required: pip install uv"; exit 1; }

echo "==> installing the locked dependency set"
uv sync

echo "==> building the retrieval index"
uv run ap-agent ingest

echo "==> unit and contract tiers (no model, no network)"
uv run pytest tests/unit tests/contract -q

echo "==> fixture cases and retrieval quality"
uv run ap-agent eval

echo
echo "Ready. Try:"
echo "  uv run ap-agent run fixtures/cases/FIN-001.json"
echo "  uv run ap-agent serve"
