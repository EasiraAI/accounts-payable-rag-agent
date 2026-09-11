# ADR-0005: LLM provider abstraction and where the model is allowed to act

**Status:** Accepted. **Date:** 2026-09-11.

## Context

The brief requires an actual LLM or an abstraction that supports one, provider
configuration outside orchestration code, validation of all model outputs, deterministic
arithmetic in code, and tests that separate model-dependent runs from stable ones.

## Decision

### Adapter interface

```
class LLMClient(Protocol):
    def complete_structured(self, *, system: str, user: str,
                            schema: type[BaseModel], max_tokens: int) -> BaseModel: ...
```

Two implementations:

- `AnthropicClient`: Claude via the official SDK, using tool-use with the pydantic JSON
  schema as the single forced tool so the response is structured by construction.
  Model, max tokens, timeout and retries come from `Settings` (env or `.env`).
- `FakeClient`: returns canned, schema-valid responses keyed by phase and case. Used by
  unit, contract and default eval tiers. Also supports fault modes (`malformed_json`,
  `schema_violation`, `timeout`) to test the repair and failure paths.

Provider selection is a single string setting, `AP_LLM_PROVIDER`. Adding a second real
provider (for example Bedrock-hosted Claude or an OpenAI model) means one new adapter
file; nothing in orchestration changes.

### Where the model is used, and where it is not

| Step | Model used? | Why |
|---|---|---|
| Query formulation for retrieval | No, templates from case fields | Deterministic, testable, no injection surface |
| Evidence synthesis (facts from chunks, with citations) | Yes, structured | Language understanding is the model's job |
| Reconciliation arithmetic and tolerance checks | No, `Decimal` engine | FIN-POL-002 §5 |
| Duplicate, vendor-status, authority rules | No, rule engine | Policy is code |
| Risk narrative, assumptions, inferences, confidence | Yes, structured | Judgement and explanation |
| Final outcome | No, engine; model may only tighten | Bounded decisions |
| Calling `submit_finance_decision` | No, orchestrator after approval | Deny by default |

### Output validation

Every call goes through `complete_structured`, which parses into the requested pydantic
model. On `ValidationError`, the adapter makes one repair call that includes the error
text; a second failure raises `ModelOutputInvalid`, which the orchestrator records as a
`FAILED` run with the raw (redacted) output in the event log. There is no free-text path.

## Rationale

Choosing Claude as the default follows from the requirement to use a real LLM plus
this system's trust model: forced tool-use gives structured output without a
post-processing parser, and the API supports the long, fenced evidence blocks used here.
The fake adapter is not a mock of convenience; it is the mechanism that lets the safety
properties (gating, idempotency, injection resistance) be asserted in CI without cost or
flakiness. The model's confinement to two narrative steps is what makes the deterministic
claims in the brief true rather than aspirational.

## Consequences

- `src/ap_agent/llm/{base.py, anthropic_client.py, fake_client.py, prompts.py}`.
- `prompts.py` owns the fencing function described in the trust-boundaries skill.
- Live eval tier is optional and clearly labelled "requires external access".
