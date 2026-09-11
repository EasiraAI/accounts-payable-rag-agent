"""Environment-driven settings.

Provider and model identifiers live here and nowhere else. Orchestration code reads
``Settings`` and never names a vendor, which is what makes the model swappable without
touching control flow (see docs/adr/0005-llm-provider-abstraction.md).

Precedence: process environment, then ``.env``, then the defaults below. Defaults are
chosen so that ``pytest`` and the fixture evaluation run with no environment at all.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

RetrievalMode = Literal["bm25", "hybrid"]
LLMProvider = Literal["anthropic", "fake"]

_REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """Runtime configuration for every layer of the agent."""

    model_config = SettingsConfigDict(
        env_prefix="AP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # ---- Model provider -------------------------------------------------------------
    llm_provider: LLMProvider = "fake"
    llm_model: str = "claude-sonnet-5"
    llm_max_tokens: int = Field(default=2048, ge=256, le=32_000)
    llm_timeout_seconds: float = Field(default=30.0, gt=0)
    llm_max_retries: int = Field(default=2, ge=0, le=5)
    # Read from the provider's conventional variable, not AP_-prefixed, so that standard
    # tooling and CI secrets work unchanged. Never logged; never written to disk.
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")

    # ---- Agent budget ---------------------------------------------------------------
    #
    # Both derived from the phase plan rather than chosen as round numbers, so exhausting
    # either means something genuinely unexpected happened. See
    # ap_agent.orchestration.phases for the attempt-by-attempt derivation.
    max_steps: int = Field(default=12, ge=1, le=100)
    max_tool_calls: int = Field(default=16, ge=1, le=100)

    # ---- Retrieval ------------------------------------------------------------------
    corpus_dir: Path = _REPO_ROOT / "finance_rag_corpus"
    index_dir: Path = _REPO_ROOT / "data" / "index"
    retrieval_top_k: int = Field(default=6, ge=1, le=50)
    retrieval_mode: RetrievalMode = "bm25"
    # Multiplier applied to a superseded document's lexical score. Policy choice, not a
    # learned value: FIN-POL-003-OLD must rank below FIN-POL-003 for the same query.
    superseded_score_factor: float = Field(default=0.3, ge=0.0, le=1.0)

    # ---- Persistence and observability ----------------------------------------------
    db_path: Path = _REPO_ROOT / "data" / "runtime" / "ap_agent.db"
    log_level: str = "INFO"
    log_format: Literal["json", "text"] = "json"
    event_log_path: Path | None = None

    # ---- Tool reliability -----------------------------------------------------------
    tool_timeout_seconds: float = Field(default=2.0, gt=0)
    tool_max_retries: int = Field(default=2, ge=0, le=5)
    tool_retry_backoff_seconds: float = Field(default=0.05, ge=0.0)

    @field_validator("log_level")
    @classmethod
    def _normalise_log_level(cls, value: str) -> str:
        upper = value.upper()
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
        if upper not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}")
        return upper

    def require_api_key(self) -> str:
        """Return the provider key or fail loudly.

        Called only by the live adapter. Keeping the check here means a missing key is a
        configuration error with a clear message rather than an authentication error from
        inside the provider SDK.
        """
        if not self.anthropic_api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Either export it or run with "
                "AP_LLM_PROVIDER=fake for the deterministic tier."
            )
        return self.anthropic_api_key


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton.

    Cached because settings are immutable for the lifetime of a process; tests that need a
    different configuration construct ``Settings(...)`` directly or call
    ``reset_settings_cache()``.
    """
    return Settings()


def reset_settings_cache() -> None:
    """Drop the cached settings. Test-support only."""
    get_settings.cache_clear()
