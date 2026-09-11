---
name: run-evals
description: Exact commands and expected outputs for the test tiers (unit, contract, eval) and how to interpret failures. Use before claiming any fixture passes or before delivery.
---

# Running the test tiers

| Tier | Command | Model calls | Purpose |
|---|---|---|---|
| Unit | `pytest tests/unit -q` | none | rule engine, chunker, redaction, idempotency |
| Contract | `pytest tests/contract -q` | none | tool schemas, API shapes, persistence round-trip |
| Eval (fake LLM) | `python -m ap_agent.cli eval --provider fake` | none | FIN-001..005 end to end, deterministic |
| Eval (live) | `python -m ap_agent.cli eval --provider anthropic` | yes | same cases through the real model |

Via the API: `GET /evaluations` runs the fake-provider tier and returns pass/fail per case.

Expected: every tier exits 0. The eval command prints a table with columns
`case | expected | actual | PASS/FAIL | steps | tool_calls | duration_ms` and writes
`docs/samples/eval_report.json`.

If a case fails:
1. Read `docs/samples/<case>_transcript.json`; find the first event whose payload
   diverges from the `fixture-cases` skill expectations.
2. Fix in the engine or fixture, never by loosening the assertion.
3. Re-run the unit tier first, then the eval tier.

Never report PASS from memory. Paste the command output.
