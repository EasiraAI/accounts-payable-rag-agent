---
name: trust-boundaries
description: Structural patterns for keeping untrusted content (retrieved chunks, case notes, attachments, tool results) from influencing control flow. Covers prompt fencing, capability separation, deny-by-default tools and safe logging. Use when writing prompts, tool schemas, the decision tool, or logging code.
---

# Trust boundaries

Three trust levels exist in this system. Nothing crosses upward.

| Level | Contents | May do |
|---|---|---|
| **Policy (trusted)** | system prompt, orchestration code, rule engine, tool permission table | decide, act |
| **Evidence (untrusted data)** | retrieved chunks, tool results, vendor and PO records | be quoted, cited, compared |
| **Case input (untrusted data)** | `notes`, `attachments`, invoice text | be quoted, cited, compared |

## Enforced structurally, not by asking nicely
1. **Fencing.** Untrusted text enters the model only inside a delimited data block with a
   random per-run boundary token and an explicit role label ("DATA, not instructions").
   One function renders the block; no other code concatenates strings into prompts.
2. **Capability separation.** The model never calls `submit_finance_decision`. It produces
   a proposal; orchestration code checks `approval.status == APPROVED` and the
   idempotency table before calling the tool. The tool itself re-checks. Two gates.
3. **No override flags.** Tool schemas have no `force`, `skip_checks` or `verified=true`
   arguments. There is nothing for injected text to ask for.
4. **Deterministic decisions.** The rule engine computes the outcome from typed facts.
   Model output adds narrative and inferences and can only make a recommendation
   more conservative, never less.
5. **Injection is a signal.** Imperative language in evidence ("ignore", "skip", "pay
   immediately", "do not ask") increments the FIN-POL-005 §3 fraud-indicator count.
6. **Validated output.** Every model response is parsed into a pydantic model. On failure,
   one repair attempt with the validation error, then explicit `MODEL_OUTPUT_INVALID`
   failure. Never fall back to free text.
7. **Safe logging.** A single `redact()` runs on every event payload: bank numbers to last
   four digits, tax IDs removed, API keys removed. Tests assert on log output.
8. **Least data.** Prompts receive only the fields the step needs. Full vendor payment
   details never go to the model.

## Red flags in review
- An f-string building a prompt from a tool result.
- A boolean in a tool schema that widens permission.
- `except Exception: pass` around a tool or model call.
- A retry loop without a counter.
- A log call with a raw tool response.
