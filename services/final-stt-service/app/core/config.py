"""Environment-driven final STT configuration."""

from __future__ import annotations

import socket
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FINAL_STT_",
        env_file=".env",
        extra="ignore",
        protected_namespaces=("settings_",),
    )

    environment: str = Field(default="local", pattern="^(local|test|staging|production)$")
    model_name: str = "large-v3"
    model_revision: str = "main"
    model_sha256: str = ""
    model_path: Path | None = None
    device: str = Field(default="cuda", pattern="^(cpu|cuda|auto)$")
    compute_type: str = "float16"
    language: str = Field(default="tr", min_length=2, max_length=5)
    beam_size: int = Field(default=1, ge=1, le=10)
    vad_filter: bool = True
    chunk_min_sec: float = Field(default=10.0, ge=1.0, le=30.0)
    chunk_max_sec: float = Field(default=15.0, ge=1.0, le=60.0)
    duration_tolerance_sec: float = Field(default=1.0, ge=0.0, le=5.0)

    audio_root: Path = Path("/audio")
    redis_enabled: bool = False
    redis_url: str = "redis://localhost:6379/0"
    redis_input_stream: str = "stt:final:jobs"
    redis_result_stream: str = "stt:final:results"
    redis_dead_letter_stream: str = "stt:final:dead"
    redis_consumer_group: str = "final-stt-service"
    redis_consumer_name: str = Field(default_factory=socket.gethostname)
    redis_block_ms: int = Field(default=2000, ge=100, le=60000)
    redis_batch_size: int = Field(default=1, ge=1, le=10)
    redis_result_maxlen: int = Field(default=10000, ge=100, le=1000000)
    log_level: str = "INFO"

    @model_validator(mode="after")
    def validate_runtime_contract(self) -> Settings:
        if self.chunk_min_sec >= self.chunk_max_sec:
            raise ValueError("chunk_min_sec must be lower than chunk_max_sec")
        if self.environment in {"staging", "production"}:
            if not self.model_sha256:
                raise ValueError("model_sha256 is required in staging/production")
            if self.model_path is None:
                raise ValueError("model_path is required in staging/production")
        return self


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
