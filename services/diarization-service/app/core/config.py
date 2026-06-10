"""diarization-service configuration.

Pydantic Settings, env prefix `DIA_`. CPU-first PoC (issue #48): GPU is decided
after the M5 performance work, mirroring the live-stt discipline. The default
backend is a deterministic **mock** so the skeleton runs without the heavy
pyannote.audio model or a Hugging Face token; the real pipeline is wired later.

No voiceprint / biometric enrolment in this phase (separate consent flow).
"""

from __future__ import annotations

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Service runtime settings.

    Env vars (prefix `DIA_`):
      DIA_MODEL_NAME      pyannote pipeline id (default: pyannote/speaker-diarization-3.1)
      DIA_DEVICE          cpu / cuda (default: cpu — PoC)
      DIA_BACKEND         mock / pyannote (default: mock)
      DIA_HF_TOKEN        Hugging Face token (REQUIRED when backend=pyannote;
                          gated model access — never logged)
      DIA_MAX_AUDIO_MB    50 (default — DoS guard)
      DIA_MAX_SPEAKERS    10 (default — cardinality cap)
      DIA_LOG_LEVEL       INFO (default)
      DIA_REQUEST_TIMEOUT 60 (default — sec, hard cap)
      DIA_MOCK_NUM_SPEAKERS 2 (mock backend speaker count)
      DIA_MOCK_TURN_SEC     2.5 (mock backend turn length)
      DIA_MOCK_DEFAULT_DURATION_SEC 6.0 (fallback when WAV duration unreadable)
    """

    model_config = SettingsConfigDict(
        env_prefix="DIA_",
        env_file=".env",
        extra="ignore",
        protected_namespaces=("settings_",),
    )

    model_name: str = Field(default="pyannote/speaker-diarization-3.1")
    device: str = Field(default="cpu", description="cpu / cuda")
    backend: str = Field(default="mock", pattern="^(mock|pyannote)$")
    hf_token: str = Field(default="", repr=False, description="HF token for gated pyannote models")
    max_audio_mb: int = Field(default=50, ge=1, le=500)
    max_speakers: int = Field(default=10, ge=1, le=50)
    log_level: str = Field(default="INFO")
    request_timeout: int = Field(default=60, ge=1, le=300)
    mock_num_speakers: int = Field(default=2, ge=1, le=50)
    mock_turn_sec: float = Field(default=2.5, gt=0.0, le=60.0)
    mock_default_duration_sec: float = Field(default=6.0, gt=0.0, le=3600.0)

    @model_validator(mode="after")
    def _pyannote_requires_token(self) -> Settings:
        """Fail fast at startup: the gated pyannote pipeline needs a HF token."""
        if self.backend == "pyannote" and not self.hf_token:
            raise ValueError(
                "DIA_BACKEND=pyannote requires DIA_HF_TOKEN " "(gated Hugging Face model access)"
            )
        return self


_settings: Settings | None = None


def get_settings() -> Settings:
    """Cached settings singleton."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
