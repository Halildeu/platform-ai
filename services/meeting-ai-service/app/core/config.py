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

import base64
import binascii
import json
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
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

    # #247 — durable meeting-service analysis-result delivery (default-off).
    ingestion_enabled: bool = Field(default=False)
    meeting_service_base_url: str = Field(default="http://meeting-service:8080")
    meeting_service_token_url: str = Field(default="")
    meeting_service_client_id: str = Field(default="")
    meeting_service_client_secret: SecretStr = Field(default=SecretStr(""))
    meeting_service_audience: str = Field(default="meeting-service")
    # The service-token PERMISSION(s), sent as the auth-service `permissions` form
    # param (repeated) — NOT an OAuth2 `scope` (auth-service ignores `scope` and
    # requires `audience`). Comma-separated for multiple; env name kept for back-compat.
    meeting_service_scope: str = Field(default="meeting:analysis-result:write")
    meeting_service_tls_mode: Literal["server", "mutual"] = Field(default="server")
    meeting_service_tls_ca_path: Path | None = Field(default=None)
    meeting_service_tls_client_cert_path: Path | None = Field(default=None)
    meeting_service_tls_client_key_path: Path | None = Field(default=None)
    meeting_service_tls_reload_interval_sec: float = Field(default=60.0, ge=1.0, le=3600.0)

    @property
    def meeting_service_permissions(self) -> list[str]:
        """Split the comma-separated permission string into the list auth-service expects."""
        return [p.strip() for p in self.meeting_service_scope.split(",") if p.strip()]

    ingestion_store_path: Path = Field(default=Path("data/analysis-delivery.sqlite3"))
    ingestion_active_key_id: str = Field(default="")
    ingestion_encryption_keys_json: SecretStr = Field(default=SecretStr(""))
    ingestion_timeout_sec: float = Field(default=10.0, ge=0.1, le=120.0)
    ingestion_max_attempts: int = Field(default=8, ge=1, le=100)
    ingestion_base_backoff_sec: float = Field(default=1.0, ge=0.1, le=300.0)
    ingestion_max_backoff_sec: float = Field(default=300.0, ge=1.0, le=86_400.0)
    ingestion_jitter_ratio: float = Field(default=0.2, ge=0.0, le=1.0)
    ingestion_poll_interval_sec: float = Field(default=1.0, ge=0.05, le=60.0)
    ingestion_lease_sec: float = Field(default=30.0, ge=1.0, le=3_600.0)
    ingestion_shutdown_grace_sec: float = Field(default=5.0, ge=0.1, le=120.0)
    ingestion_max_rows: int = Field(default=10_000, ge=1, le=1_000_000)
    ingestion_stale_after_sec: float = Field(default=300.0, ge=1.0, le=604_800.0)
    prompt_version: str | None = Field(default=None, max_length=64)

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
        """Stable producer provenance when no explicit prompt version is configured."""
        return self.prompt_version or f"{self.backend}-v1"

    def ingestion_encryption_keys(self) -> dict[str, bytes]:
        """Decode the secret AES-256-GCM keyring without exposing it in repr/logs."""
        raw = self.ingestion_encryption_keys_json.get_secret_value()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("MAI_INGESTION_ENCRYPTION_KEYS_JSON must be valid JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError("MAI_INGESTION_ENCRYPTION_KEYS_JSON must be a JSON object")

        decoded: dict[str, bytes] = {}
        for key_id, encoded in parsed.items():
            if not isinstance(key_id, str) or not key_id or not isinstance(encoded, str):
                raise ValueError("ingestion encryption keyring entries must be non-empty strings")
            try:
                key = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError(
                    f"ingestion encryption key {key_id!r} is not valid base64"
                ) from exc
            if len(key) != 32:
                raise ValueError(f"ingestion encryption key {key_id!r} must decode to 32 bytes")
            decoded[key_id] = key
        return decoded

    @model_validator(mode="after")
    def _enforce_ingestion_boundary(self) -> Settings:
        """Fail at startup when durable delivery cannot meet its security contract."""
        if not self.ingestion_enabled:
            return self
        if not (
            self.meeting_service_token_url
            and self.meeting_service_client_id
            and self.meeting_service_client_secret.get_secret_value()
        ):
            raise ValueError(
                "MAI_INGESTION_ENABLED=True requires token URL, client id, and client secret"
            )
        for name, value in (
            ("MAI_MEETING_SERVICE_BASE_URL", self.meeting_service_base_url),
            ("MAI_MEETING_SERVICE_TOKEN_URL", self.meeting_service_token_url),
        ):
            parsed = urlsplit(value)
            if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
                raise ValueError(
                    f"{name} must be an absolute HTTPS URL without embedded credentials"
                )

        tls_paths = {
            "MAI_MEETING_SERVICE_TLS_CA_PATH": self.meeting_service_tls_ca_path,
            "MAI_MEETING_SERVICE_TLS_CLIENT_CERT_PATH": (self.meeting_service_tls_client_cert_path),
            "MAI_MEETING_SERVICE_TLS_CLIENT_KEY_PATH": self.meeting_service_tls_client_key_path,
        }
        if self.meeting_service_tls_mode == "mutual" and any(
            path is None for path in tls_paths.values()
        ):
            raise ValueError(
                "MAI_MEETING_SERVICE_TLS_MODE=mutual requires CA, client certificate, and "
                "client key paths"
            )
        for name, path in tls_paths.items():
            if path is None:
                continue
            if not path.is_absolute():
                raise ValueError(f"{name} must be an absolute path")
            if not path.is_file():
                raise ValueError(f"{name} must reference a readable file")
        if not self.ingestion_store_path.is_absolute():
            raise ValueError("MAI_INGESTION_STORE_PATH must be absolute when ingestion is enabled")
        keys = self.ingestion_encryption_keys()
        if not self.ingestion_active_key_id or self.ingestion_active_key_id not in keys:
            raise ValueError(
                "MAI_INGESTION_ACTIVE_KEY_ID must select a key from the encrypted keyring"
            )
        if self.ingestion_max_backoff_sec < self.ingestion_base_backoff_sec:
            raise ValueError("ingestion max backoff must be >= base backoff")
        # A cold delivery can spend one timeout acquiring a token and a second
        # timeout posting the payload. Keep ownership for the whole attempt so
        # another worker cannot reclaim and duplicate it mid-flight.
        if self.ingestion_lease_sec <= 2 * self.ingestion_timeout_sec:
            raise ValueError("ingestion lease must be greater than two HTTP timeout windows")
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
