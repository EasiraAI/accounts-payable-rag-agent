"""Model adapters, prompt construction and the schemas the model may produce."""

from ap_agent.config.settings import Settings
from ap_agent.llm.base import LLMClient, ModelCallRecord
from ap_agent.llm.fake_client import FAULT_MODES, FakeLLMClient
from ap_agent.llm.prompts import (
    SYSTEM_PROMPT,
    build_evidence_prompt,
    build_recommendation_prompt,
    fence,
    new_boundary_nonce,
    render_case_input,
    render_chunks,
    render_computed_findings,
)
from ap_agent.llm.schemas import (
    OUTCOME_SEVERITY,
    EvidenceSynthesis,
    ModelConfidence,
    ModelFact,
    ModelInference,
    ModelUnknown,
    RecommendationNarrative,
    tightens,
)

__all__ = [
    "FAULT_MODES",
    "OUTCOME_SEVERITY",
    "SYSTEM_PROMPT",
    "EvidenceSynthesis",
    "FakeLLMClient",
    "LLMClient",
    "ModelCallRecord",
    "ModelConfidence",
    "ModelFact",
    "ModelInference",
    "ModelUnknown",
    "RecommendationNarrative",
    "build_evidence_prompt",
    "build_llm_client",
    "build_recommendation_prompt",
    "fence",
    "new_boundary_nonce",
    "render_case_input",
    "render_chunks",
    "render_computed_findings",
    "tightens",
]


def build_llm_client(settings: Settings) -> LLMClient:
    """Select the adapter named in configuration.

    The single place a provider name is turned into an implementation. Orchestration receives
    an ``LLMClient`` and never learns which one, which is what makes the provider swappable
    without touching control flow (see docs/adr/0005-llm-provider-abstraction.md).
    """
    if settings.llm_provider == "fake":
        return FakeLLMClient()
    if settings.llm_provider == "anthropic":
        from ap_agent.llm.anthropic_client import AnthropicClient

        return AnthropicClient(settings)
    raise ValueError(f"unknown LLM provider {settings.llm_provider!r}")
