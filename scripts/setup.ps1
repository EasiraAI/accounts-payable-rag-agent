# One-command local setup for Windows. Idempotent: safe to re-run.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Error "uv is required: pip install uv"
}

Write-Host "==> installing the locked dependency set"
uv sync

Write-Host "==> building the retrieval index"
uv run ap-agent ingest

Write-Host "==> unit and contract tiers (no model, no network)"
uv run pytest tests/unit tests/contract -q

Write-Host "==> fixture cases and retrieval quality"
uv run ap-agent eval

Write-Host ""
Write-Host "Ready. Try:"
Write-Host "  uv run ap-agent run fixtures/cases/FIN-001.json"
Write-Host "  uv run ap-agent serve"
