"""live-stt-service configuration.

Pydantic Settings ile env-driven config. Whisper model + compute parametreleri
runtime'da pin'lenir. Drift sıfır tolerans — model değişimi ADR + Codex
consensus gerektirir.
"""

from __future__ import annotations

from dataclasses import dataclass

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
      STT_WORKER_MAX_WORKERS 1 (default, subprocess worker pool size)
      STT_WORKER_BACKEND    process / inline (default: process)
      STT_WORKER_KILL_GRACE_SEC 2.0 (default, terminate -> kill grace)
      STT_REQUEST_TIMEOUT  60 (default — sec, hard cap)
      STT_WORKER_VRAM_BUDGET_MB     0 (default=disabled; >0 enables CUDA admission)
      STT_WORKER_VRAM_PER_WORKER_MB 2100 (default; measured medium/fp16 on RTX 4070, #42)
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
    worker_max_workers: int = Field(default=1, ge=1, le=8)
    worker_backend: str = Field(default="process", pattern="^(process|inline)$")
    worker_kill_grace_sec: float = Field(default=2.0, ge=0.0, le=30.0)
    # GPU VRAM admission (#42). Disabled by default (budget 0). The per-worker
    # figure is the value MEASURED on the target GPU, never an auto-guess.
    worker_vram_budget_mb: int = Field(default=0, ge=0, le=80000)
    worker_vram_per_worker_mb: int = Field(default=2100, ge=1, le=80000)


_settings: Settings | None = None


def get_settings() -> Settings:
    """Cached settings singleton."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


@dataclass(frozen=True)
class WorkerCountPlan:
    """Outcome of GPU VRAM admission for the worker pool size."""

    requested: int
    effective: int
    affordable: int | None  # None when the budget guard is not applied
    clamped: bool


def resolve_worker_count(settings: Settings) -> WorkerCountPlan:
    """Decide how many workers to start, honoring an optional GPU VRAM budget.

    The guard only engages on CUDA when `worker_vram_budget_mb > 0`; otherwise
    the requested `worker_max_workers` is used unchanged (CPU and the default
    config are unaffected). The per-worker VRAM figure is operator-supplied
    (measured on the target GPU, #42) — no hard-coded estimate drives a clamp.
    """
    requested = settings.worker_max_workers
    guard_active = (
        settings.device == "cuda"
        and settings.worker_vram_budget_mb > 0
        and settings.worker_vram_per_worker_mb > 0
    )
    if not guard_active:
        return WorkerCountPlan(
            requested=requested, effective=requested, affordable=None, clamped=False
        )
    affordable = max(1, settings.worker_vram_budget_mb // settings.worker_vram_per_worker_mb)
    effective = min(requested, affordable)
    return WorkerCountPlan(
        requested=requested,
        effective=effective,
        affordable=affordable,
        clamped=effective < requested,
    )
