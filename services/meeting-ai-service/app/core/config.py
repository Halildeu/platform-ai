"""meeting-ai-service configuration.

Pydantic Settings, env prefix `MAI_`. Issue #49 skeleton.

The default backend is a deterministic **mock** that performs no LLM call, so the
skeleton runs without API keys and respects the KVKK boundary out of the box.
Real LLM backends (Option A: Anthropic/OpenAI, Option B: Ollama) are stubs here —
wiring them requires the ADR-0030 Option A/B decision and secret handling.

**PII redaction is applied to the transcript BEFORE the analyzer sees it**
(`redact_pii=True` by default), so even a real LLM backend only ever receives
redacted text.
"""

from __future__ import annotations

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Service runtime settings.

    Env vars (prefix `MAI_`):
      MAI_BACKEND           mock / anthropic / openai / ollama (default: mock)
      MAI_MODEL_NAME        provider model id (default: placeholder)
      MAI_MAX_TRANSCRIPT_CHARS 100000 (default — DoS guard)
      MAI_REDACT_PII        True (default — redact before analyzer/LLM)
      MAI_LOG_LEVEL         INFO (default)
      MAI_REQUEST_TIMEOUT   60 (default — sec, hard cap)
      MAI_SUMMARY_MAX_CHARS 280 (mock summary cap)
    """

    model_config = SettingsConfigDict(
        env_prefix="MAI_",
        env_file=".env",
        extra="ignore",
        protected_namespaces=("settings_",),
    )

    app_env: str = Field(default="dev", pattern="^(dev|test|stage|prod)$")
    backend: str = Field(default="mock", pattern="^(mock|anthropic|openai|ollama)$")
    model_name: str = Field(default="placeholder-skeleton")
    max_transcript_chars: int = Field(default=100_000, ge=1, le=2_000_000)
    redact_pii: bool = Field(default=True)
    log_level: str = Field(default="INFO")
    request_timeout: int = Field(default=60, ge=1, le=300)
    summary_max_chars: int = Field(default=280, ge=40, le=4000)
    # Option B (Ollama) settings — #54
    ollama_host: str = Field(default="http://localhost:11434")
    ollama_model: str = Field(default="llama3.1:8b")

    @property
    def effective_model(self) -> str:
        """The model actually serving requests, for honest provenance everywhere.

        For `ollama` the real model is `ollama_model` (e.g. llama3.1:8b), not the
        generic `model_name` placeholder. Used by /analyze, /health, startup log
        and eval so reported provenance is consistent (review #166).
        """
        if self.backend == "ollama":
            return self.ollama_model
        return self.model_name

    @model_validator(mode="after")
    def _enforce_kvkk_redaction_boundary(self) -> Settings:
        """Issue #49 hard requirement: PII redaction BEFORE any LLM call.

        Disabling redaction is only permitted on the in-process mock backend
        (no data leaves the service). For any real LLM backend the boundary is
        mandatory and cannot be switched off by env.
        """
        if self.backend != "mock" and not self.redact_pii:
            raise ValueError(
                "MAI_REDACT_PII=False is not allowed with a non-mock backend: "
                "the KVKK boundary requires redaction before any LLM call"
            )
        # ADR-0043 D3 (Codex 019ee9a6): the mock backend keeps redaction best-effort
        # (in-process, no LLM). It must NEVER serve a deployed env, where that would be
        # a PII-guard bypass. Hard-fail at startup in stage/prod.
        if self.app_env in {"stage", "prod"} and self.backend == "mock":
            raise ValueError(
                f"backend=mock is forbidden in app_env={self.app_env}: "
                "the deterministic mock must not serve deployed traffic (PII-guard bypass)"
            )
        return self


_settings: Settings | None = None


def get_settings() -> Settings:
    """Cached settings singleton."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
