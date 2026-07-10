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
      MAI_OLLAMA_HOST       http://localhost:11434 (Option B)
      MAI_OLLAMA_MODEL      llama3.1:8b (selectable: e.g. qwen2.5:7b-instruct)
      MAI_OLLAMA_TEMPERATURE 0.0 (deterministic extraction; NOT chat 0.8)
      MAI_OLLAMA_NUM_CTX    8192 (avoid 2048-default transcript truncation)
      MAI_OLLAMA_TOP_P      0.9
      MAI_OLLAMA_SEED       (unset = random; set int for reproducible eval)
      MAI_OLLAMA_KEEP_ALIVE 5m (unload idle model → free shared GPU VRAM)
      MAI_INGESTION_ENABLED  False (default — #244 AI-1, opt-in like #184's
                              streaming.enabled; meeting-service persistence
                              stays off until GW/SEC provisions the service
                              token and network path)
      MAI_MEETING_SERVICE_BASE_URL      http://meeting-service:8080 (default)
      MAI_MEETING_SERVICE_TOKEN_URL     Keycloak token endpoint (no default —
                                        required when ingestion is enabled)
      MAI_MEETING_SERVICE_CLIENT_ID     Keycloak client id for the
                                        client_credentials grant (GW/SEC-provisioned)
      MAI_MEETING_SERVICE_CLIENT_SECRET Keycloak client secret (GW/SEC-provisioned,
                                        read from a mounted secret, never logged)
      MAI_MEETING_SERVICE_SCOPE         meeting:analysis-result:write (default —
                                        must match #244 BE-1's required JWT scope)
      MAI_INGESTION_TIMEOUT_SEC         10 (default — per-attempt HTTP timeout)
      MAI_INGESTION_MAX_ATTEMPTS        3 (default — retry-safe per #244 AI-1)
      MAI_PROMPT_VERSION                backend-specific default (mock-v1 /
                                        ollama-v1) — see Settings.prompt_version
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
    # Ollama decoding controls (#162 — fair/reproducible G-INT eval, see ADR-0034).
    # Defaults target DETERMINISTIC STRUCTURED EXTRACTION, not chat:
    #   - temperature 0      → greedy, repeatable (Ollama default 0.8 made the eval
    #                          swing run-to-run: 95.8%→81.2%, ADR-0034 variance note).
    #   - num_ctx 8192       → meeting transcripts are NOT silently truncated to
    #                          Ollama's 2048-token default (the real recall killer).
    #   - seed (optional)    → set for reproducible eval; multi-seed measures variance.
    #   - keep_alive         → unload after idle so the 8 GB GPU is freed for the
    #                          STT/diarization/emotion models that share it.
    ollama_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    ollama_num_ctx: int = Field(default=8192, ge=512, le=131072)
    ollama_top_p: float = Field(default=0.9, ge=0.0, le=1.0)
    ollama_seed: int | None = Field(default=None)
    ollama_keep_alive: str = Field(default="5m")

    # #244 AI-1 — meeting-service aggregate-ingestion client (default-off).
    ingestion_enabled: bool = Field(default=False)
    meeting_service_base_url: str = Field(default="http://meeting-service:8080")
    meeting_service_token_url: str = Field(default="")
    meeting_service_client_id: str = Field(default="")
    meeting_service_client_secret: str = Field(default="")
    meeting_service_scope: str = Field(default="meeting:analysis-result:write")
    ingestion_timeout_sec: int = Field(default=10, ge=1, le=120)
    ingestion_max_attempts: int = Field(default=3, ge=1, le=10)
    prompt_version: str | None = Field(default=None)

    def ollama_options(self) -> dict[str, object]:
        """Decoding options for Ollama `/api/generate` (deterministic extraction).

        One source of truth for both the analyze and ask paths so a single
        ``MAI_OLLAMA_*`` change applies everywhere. ``seed`` is omitted unless set,
        matching Ollama's "random seed" default behaviour.
        """
        opts: dict[str, object] = {
            "temperature": self.ollama_temperature,
            "num_ctx": self.ollama_num_ctx,
            "top_p": self.ollama_top_p,
        }
        if self.ollama_seed is not None:
            opts["seed"] = self.ollama_seed
        return opts

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

    @property
    def effective_prompt_version(self) -> str:
        """Prompt-contract version for provenance (#244 promptVersion field).

        Explicit `MAI_PROMPT_VERSION` wins; otherwise a stable per-backend
        default so `mock` and `ollama` runs are still distinguishable in
        meeting-service's ingested rows without requiring an env var.
        """
        if self.prompt_version:
            return self.prompt_version
        return f"{self.backend}-v1"

    @model_validator(mode="after")
    def _enforce_ingestion_credentials_when_enabled(self) -> Settings:
        """#244 AI-1: fail closed at startup, not on the first ingestion call.

        Mirrors `_enforce_kvkk_redaction_boundary` below — a missing
        credential should be a deploy-time config error, not a runtime
        surprise discovered only when the first meeting finishes.
        """
        if self.ingestion_enabled and not (
            self.meeting_service_token_url
            and self.meeting_service_client_id
            and self.meeting_service_client_secret
        ):
            raise ValueError(
                "MAI_INGESTION_ENABLED=True requires MAI_MEETING_SERVICE_TOKEN_URL, "
                "MAI_MEETING_SERVICE_CLIENT_ID, and MAI_MEETING_SERVICE_CLIENT_SECRET"
            )
        return self

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
