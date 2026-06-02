"""live-stt-service configuration.

Pydantic Settings ile env-driven config. Whisper model + compute parametreleri
runtime'da pin'lenir. Drift sıfır tolerans — model değişimi ADR + Codex
consensus gerektirir.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Service runtime settings.

    Env vars (prefix `STT_`):
      STT_MODEL_NAME       Whisper model adı (default: medium)
      STT_COMPUTE_TYPE     int8 / int8_float16 / float16 / float32 (default: int8)
      STT_DEVICE           cpu / cuda / auto (default: cpu — PoC)
      STT_LANGUAGE         force language code (default: tr); "auto" = detect
      STT_BEAM_SIZE        5 (default — accuracy/speed trade-off)
      STT_VAD_FILTER       True (default — Whisper built-in VAD)
      STT_MAX_AUDIO_MB     50 (default — DoS guard)
      STT_LOG_LEVEL        INFO (default)
      STT_REQUEST_TIMEOUT  60 (default — sec, hard cap)
    """

    model_config = SettingsConfigDict(
        env_prefix="STT_",
        env_file=".env",
        extra="ignore",
        # `model_name` clashes with pydantic's protected `model_` namespace; opt out.
        protected_namespaces=("settings_",),
    )

    model_name: str = Field(default="medium", description="Whisper model")
    compute_type: str = Field(default="int8", description="quantization")
    device: str = Field(default="cpu", description="cpu / cuda / auto")
    language: str = Field(default="tr", description="ISO 639-1 or 'auto'")
    beam_size: int = Field(default=5, ge=1, le=10)
    vad_filter: bool = Field(default=True)
    max_audio_mb: int = Field(default=50, ge=1, le=500)
    log_level: str = Field(default="INFO")
    # Lower bound 1s allows test-suite to assert timeout behaviour without slow sleeps;
    # production deploys should keep the default (60s) or higher per K8s readiness probe.
    request_timeout: int = Field(default=60, ge=1, le=300)


_settings: Settings | None = None


def get_settings() -> Settings:
    """Cached settings singleton."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
