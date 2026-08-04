"""live-stt-service configuration.

Pydantic Settings ile env-driven config. Whisper model + compute parametreleri
runtime'da pin'lenir. Drift sıfır tolerans — model değişimi ADR + Codex
consensus gerektirir.
"""

from __future__ import annotations

import re
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from pydantic import Field, model_validator
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
      STT_CONDITION_ON_PREVIOUS_TEXT False (default — suppress cross-segment hallucination drift)
      STT_NO_SPEECH_THRESHOLD 0.75 (default — align sync /transcribe with live stream)
      STT_LOG_PROB_THRESHOLD -1.0 (default — reject low-confidence decode paths)
      STT_COMPRESSION_RATIO_THRESHOLD 2.4 (default — reject repetitive decode paths)
      STT_MAX_AUDIO_MB     50 (default — DoS guard)
      STT_LOG_LEVEL        INFO (default)
      STT_WORKER_MAX_WORKERS 1 (default, subprocess worker pool size)
      STT_WORKER_BACKEND    process / inline / shared (default: process)
                            shared = single process, one shared WhisperModel,
                            K concurrent CUDA streams (#42 multi-stream).
      STT_WORKER_KILL_GRACE_SEC 2.0 (default, terminate -> kill grace)
      STT_REQUEST_TIMEOUT  60 (default — sec, hard cap)
      STT_WORKER_VRAM_BUDGET_MB     0 (default=disabled; >0 enables CUDA admission)
      STT_WORKER_VRAM_PER_WORKER_MB 2100 (default; measured medium/fp16 on RTX 4070, #42)
      STT_CHUNK_CONSUMER_ENABLED  False (default; PR-stt-04 #137 gateway stream consumer)
      STT_REDIS_URL               redis://localhost:6379/0 (staging-sw Redis)
      STT_CHUNK_STREAM_PREFIX     audio:chunks:p (ADR-0031 D3, 32 partitions)
      STT_CHUNK_CONSUMER_GROUP    live-stt-v1
      STT_LIVE_BEAM_SIZE          1 (default; low-latency draft)
      STT_FINAL_BEAM_SIZE         1 (default; ADR-0031 final revision)
      STT_STREAM_LIVE_VAD_FILTER  False (default; production profile enables it)
      STT_STREAM_FINAL_VAD_FILTER False (default; production profile enables it)
      STT_STREAM_TRANSPORT_TIMEOUT_SEC 2.0 (default; per-WebSocket-write cap)
    """

    model_config = SettingsConfigDict(
        env_prefix="STT_",
        env_file=".env",
        extra="ignore",
        # `model_name` clashes with pydantic's protected `model_` namespace; opt out.
        protected_namespaces=("settings_",),
    )

    model_name: str = Field(default="medium", description="Whisper model")
    # Model artifact identity is separate from the service version. Production
    # pins the upstream repository revision and verifies the actual model.bin
    # bytes before Whisper can load them. Local/test keeps the historical
    # floating-name convenience, but can opt into the same contract.
    environment: str = Field(default="local", pattern="^(local|test|staging|production)$")
    runtime_commit: str = Field(default="unversioned", min_length=1, max_length=40)
    model_revision: str = Field(default="unversioned", min_length=1, max_length=128)
    model_sha256: str = Field(default="", max_length=71)
    model_tree_sha256: str = Field(default="", max_length=71)
    model_path: Path | None = None
    compute_type: str = Field(default="int8", description="quantization")
    device: str = Field(default="cpu", description="cpu / cuda / auto")
    language: str = Field(default="tr", description="ISO 639-1 or 'auto'")
    beam_size: int = Field(default=5, ge=1, le=10)
    vad_filter: bool = Field(default=True)
    # Decode tuning shared by BOTH the sync /transcribe worker path and the
    # live /ws/stream draft+final path (#237). Previously the live path hard-
    # coded these as module constants, so tuning STT_NO_SPEECH_THRESHOLD et al.
    # silently moved only the sync path; they now drive both. Defaults preserve
    # the prior behavior (identical to the old live-path constants).
    condition_on_previous_text: bool = Field(default=False)
    no_speech_threshold: float = Field(default=0.75, ge=0.0, le=1.0)
    log_prob_threshold: float = Field(default=-1.0, ge=-10.0, le=10.0)
    compression_ratio_threshold: float = Field(default=2.4, ge=0.1, le=10.0)
    max_audio_mb: int = Field(default=50, ge=1, le=500)
    log_level: str = Field(default="INFO")
    # Lower bound 1s allows test-suite to assert timeout behaviour without slow sleeps;
    # production deploys should keep the default (60s) or higher per K8s readiness probe.
    request_timeout: int = Field(default=60, ge=1, le=300)
    worker_max_workers: int = Field(default=1, ge=1, le=8)
    worker_backend: str = Field(default="process", pattern="^(process|inline|shared)$")
    worker_kill_grace_sec: float = Field(default=2.0, ge=0.0, le=30.0)
    # GPU VRAM admission (#42). Disabled by default (budget 0). The per-worker
    # figure is the value MEASURED on the target GPU, never an auto-guess.
    worker_vram_budget_mb: int = Field(default=0, ge=0, le=80000)
    worker_vram_per_worker_mb: int = Field(default=2100, ge=1, le=80000)
    # #128 WebSocket streaming: two-stage models (ADR-0031: draft=medium int8,
    # final=large-v3-turbo fp16). Lazy-loaded only when /ws/stream is used, so
    # CPU/CI paths are unaffected by the cuda defaults.
    live_model_name: str = Field(default="medium", description="fast draft model")
    live_model_revision: str = Field(default="unversioned", min_length=1, max_length=128)
    live_model_sha256: str = Field(default="", max_length=71)
    live_model_tree_sha256: str = Field(default="", max_length=71)
    live_model_path: Path | None = None
    live_compute_type: str = Field(default="int8")
    live_device: str = Field(default="cuda")
    live_beam_size: int = Field(default=1, ge=1, le=10)
    # Draft inference is process-supervised in shared environments. A native
    # Whisper/CUDA hang must be killable; cancelling its asyncio waiter alone
    # would leave the model lock and a worker thread occupied indefinitely.
    stream_live_timeout_sec: float = Field(default=5.0, ge=0.1, le=30.0)
    stream_live_worker_backend: str = Field(default="process", pattern="^(process|inline)$")
    final_model_name: str = Field(
        default="deepdml/faster-whisper-large-v3-turbo-ct2",
        description="accurate final model (ADR-0031)",
    )
    final_model_revision: str = Field(default="unversioned", min_length=1, max_length=128)
    final_model_sha256: str = Field(default="", max_length=71)
    final_model_tree_sha256: str = Field(default="", max_length=71)
    final_model_path: Path | None = None
    final_compute_type: str = Field(default="float16")
    final_device: str = Field(default="cuda")
    final_beam_size: int = Field(default=1, ge=1, le=10)
    # Transcript-free verbose debug events over WS (KVKK: default off, #30).
    stream_debug: bool = Field(default=False)
    # Local/test imports keep VAD opt-in. The source-controlled GPU production
    # profile pins both roles and the Silero parameters below; production cannot
    # silently inherit faster-whisper defaults or disable either role.
    speech_gate_profile: str = Field(default="development-unpinned", max_length=64)
    speech_gate_rms_source: str = Field(
        default="source-baseline",
        pattern="^(source-baseline|host-override)$",
    )
    stream_live_vad_filter: bool = Field(default=False)
    stream_final_vad_filter: bool = Field(default=False)
    stream_vad_threshold: float = Field(default=0.35, ge=0.0, le=1.0)
    stream_vad_min_speech_duration_ms: int = Field(default=100, ge=32, le=2000)
    stream_vad_min_silence_duration_ms: int = Field(default=300, ge=32, le=2000)
    stream_vad_speech_pad_ms: int = Field(default=100, ge=0, le=1000)
    # A final decode must have a bounded terminal time. The gateway keeps a
    # bounded source-audio history until the final's absolute sample range is
    # acknowledged; an unbounded model call would otherwise make that contract
    # impossible to size safely.
    stream_final_timeout_sec: float = Field(default=30.0, ge=1.0, le=60.0)
    # Every WebSocket write is bounded independently. This is also part of the
    # advertised EOF budget so peer backpressure cannot hold terminal drain
    # open indefinitely after the model deadline has expired.
    stream_transport_timeout_sec: float = Field(default=2.0, ge=0.05, le=10.0)
    # Production final decoding runs in a supervised child process so a native/GPU
    # hang can be terminated rather than merely abandoning a Python waiter.
    stream_final_worker_backend: str = Field(default="process", pattern="^(process|inline)$")
    stream_model_load_timeout_sec: float = Field(default=180.0, ge=1.0, le=600.0)
    # Load both streaming models at startup instead of on the first WebSocket.
    #
    # Lazy loading deadlocks against client patience: a cold load takes minutes,
    # the desktop recorder waits 10s for `ready`, and its disconnect cancels the
    # load ("WS disconnected during model load"). Every attempt then restarts
    # from zero, so the model never finishes loading and no session can ever
    # start. Preloading moves that cost to boot, where nothing is waiting.
    #
    # Runs in a background thread: startup stays non-blocking so liveness probes
    # answer immediately. The production launcher opts in explicitly; local and
    # test environments must not allocate both models merely by importing the app.
    stream_preload_models: bool = Field(default=False)
    stream_preload_max_attempts: int = Field(default=2, ge=1, le=5)
    stream_preload_retry_base_sec: float = Field(default=1.0, ge=0.1, le=30.0)
    stream_preload_readiness_budget_sec: float = Field(default=960.0, ge=1.0, le=3600.0)
    stream_recovery_poll_sec: float = Field(default=1.0, ge=0.1, le=30.0)
    # #128 WebSocket streaming cadence/commit tuning. These env-backed values
    # stay bounded so a bad rollout cannot turn partials off or flood finals.
    live_infer_interval_ms: int = Field(default=700, ge=1, le=5000)
    live_window_sec: float = Field(default=2.0, ge=0.1, le=10.0)
    final_window_sec: float = Field(default=6.0, ge=1.0, le=60.0)
    forced_commit_sec: float = Field(default=5.0, ge=0.1, le=60.0)
    silence_commit_sec: float = Field(default=0.7, ge=0.1, le=5.0)
    tail_overlap_sec: float = Field(default=0.25, ge=0.0, le=5.0)
    # Electron/WebAudio microphone frames are much quieter than the original GPU
    # demo fixtures: real desktop speech commonly lands around RMS 0.002-0.005.
    silence_rms: float = Field(default=0.0005, ge=0.0001, le=0.05)
    min_speech_rms: float = Field(default=0.0005, ge=0.0001, le=0.05)
    min_infer_sec: float = Field(default=0.35, ge=0.01, le=5.0)
    debug_every_sec: float = Field(default=1.0, ge=0.1, le=10.0)
    # Comma-separated allowed origins for the browser streaming demo; empty =
    # CORS middleware not installed (internal service default).
    cors_origins: str = Field(default="")
    # ── PR-stt-04 (#137): gateway Redis Streams chunk consumer ────────────────
    # Contract fixed by platform-backend PR #534 (ADR-0031 D3): 32 partitions
    # audio:chunks:p00..p31, consumer group live-stt-v1, messageId dedup,
    # XACK + bounded trim on the consumer, XAUTOCLAIM crash recovery.
    # Default OFF — CI/CPU paths and the HTTP/WS API are unaffected.
    chunk_consumer_enabled: bool = Field(default=False)
    redis_url: str = Field(default="redis://localhost:6379/0")
    chunk_stream_prefix: str = Field(default="audio:chunks:p")
    chunk_partition_count: int = Field(default=32, ge=1, le=100)
    chunk_consumer_group: str = Field(default="live-stt-v1")
    chunk_consumer_name: str = Field(default_factory=socket.gethostname)
    chunk_block_ms: int = Field(default=2000, ge=100, le=60000)
    chunk_batch_size: int = Field(default=16, ge=1, le=1000)
    chunk_dedup_cache_size: int = Field(default=8192, ge=64, le=1_000_000)
    chunk_claim_idle_ms: int = Field(default=60_000, ge=1000, le=3_600_000)
    chunk_claim_every_loops: int = Field(default=30, ge=1, le=10_000)
    chunk_trim_maxlen: int = Field(default=10_000, ge=100, le=1_000_000)

    @model_validator(mode="after")
    def validate_stream_tuning(self) -> Self:
        """Keep model provenance and low-latency knobs internally consistent."""
        model_identities = (
            (
                "model", self.model_revision, self.model_sha256,
                self.model_tree_sha256, self.model_path,
            ),
            (
                "live_model",
                self.live_model_revision,
                self.live_model_sha256,
                self.live_model_tree_sha256,
                self.live_model_path,
            ),
            (
                "final_model",
                self.final_model_revision,
                self.final_model_sha256,
                self.final_model_tree_sha256,
                self.final_model_path,
            ),
        )
        for label, _revision, digest, tree_digest, _path in model_identities:
            for field_name, value in (("sha256", digest), ("tree_sha256", tree_digest)):
                if value and not re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", value):
                    raise ValueError(
                        f"{label}_{field_name} must be a lowercase full SHA-256 digest"
                    )
        if self.environment in {"staging", "production"}:
            for label, revision, digest, tree_digest, path in model_identities:
                if not re.fullmatch(r"[0-9a-f]{40}", revision):
                    raise ValueError(
                        f"{label}_revision must be a lowercase 40-hex immutable revision "
                        "in staging/production"
                    )
                if not digest:
                    raise ValueError(f"{label}_sha256 is required in staging/production")
                if not tree_digest:
                    raise ValueError(
                        f"{label}_tree_sha256 is required in staging/production"
                    )
                if path is None:
                    raise ValueError(f"{label}_path is required in staging/production")
        if self.environment == "production" and not self.stream_preload_models:
            raise ValueError("stream_preload_models must be enabled in production")
        if self.environment == "production" and not re.fullmatch(
            r"[0-9a-f]{40}", self.runtime_commit
        ):
            raise ValueError("runtime_commit must be a lowercase 40-hex commit in production")
        if self.environment == "production":
            expected_runtime = {
                "device": (self.device, "cpu"),
                "compute_type": (self.compute_type, "int8"),
                "live_device": (self.live_device, "cuda"),
                "live_compute_type": (self.live_compute_type, "int8"),
                "final_device": (self.final_device, "cuda"),
                "final_compute_type": (self.final_compute_type, "float16"),
            }
            for label, (actual, expected) in expected_runtime.items():
                if actual != expected:
                    raise ValueError(f"{label} must be {expected} in production")
            if self.speech_gate_profile != "silero-balanced-v1":
                raise ValueError("speech_gate_profile must be silero-balanced-v1 in production")
            # Draft-lane VAD is a policy choice, not a production invariant:
            # drafts are ephemeral and always superseded by the VAD-gated
            # finals, and with silero on the 2 s live windows the draft lane
            # starves (gitops#3419 field data: 3 drafts in a 2.5-minute
            # meeting; tr-cv17 fixture <3 partials over 2x replays). The
            # durable transcript keeps its guarantee below: final-lane VAD
            # stays mandatory in production.
            if not self.stream_final_vad_filter:
                raise ValueError("stream_final_vad_filter must be enabled in production")
            expected_speech_gate = {
                "live_infer_interval_ms": (self.live_infer_interval_ms, 700),
                "live_window_sec": (self.live_window_sec, 2.0),
                "final_window_sec": (self.final_window_sec, 6.0),
                "forced_commit_sec": (self.forced_commit_sec, 5.0),
                "silence_commit_sec": (self.silence_commit_sec, 0.7),
                "tail_overlap_sec": (self.tail_overlap_sec, 0.25),
                "min_infer_sec": (self.min_infer_sec, 0.35),
                "stream_vad_threshold": (self.stream_vad_threshold, 0.35),
                "stream_vad_min_speech_duration_ms": (
                    self.stream_vad_min_speech_duration_ms,
                    100,
                ),
                "stream_vad_min_silence_duration_ms": (
                    self.stream_vad_min_silence_duration_ms,
                    300,
                ),
                "stream_vad_speech_pad_ms": (self.stream_vad_speech_pad_ms, 100),
            }
            for gate_label, (gate_actual, gate_expected) in expected_speech_gate.items():
                if gate_actual != gate_expected:
                    raise ValueError(f"{gate_label} must be {gate_expected} in production")
            if self.stream_vad_min_silence_duration_ms >= int(self.live_window_sec * 1000):
                raise ValueError(
                    "stream_vad_min_silence_duration_ms must be shorter than live_window_sec"
                )
        retry_wait_sec = self.stream_preload_retry_base_sec * (
            (2 ** (self.stream_preload_max_attempts - 1)) - 1
        )
        preload_worst_case_sec = 2 * (
            self.stream_preload_max_attempts
            * (self.stream_model_load_timeout_sec + (2 * self.worker_kill_grace_sec))
            + retry_wait_sec
        )
        if preload_worst_case_sec > self.stream_preload_readiness_budget_sec:
            raise ValueError(
                "stream preload worst-case timeout exceeds stream_preload_readiness_budget_sec"
            )
        if self.min_speech_rms < self.silence_rms:
            raise ValueError("min_speech_rms must be >= silence_rms")
        if self.min_infer_sec > self.live_window_sec:
            raise ValueError("min_infer_sec must be <= live_window_sec")
        if self.tail_overlap_sec >= self.final_window_sec:
            raise ValueError("tail_overlap_sec must be < final_window_sec")
        terminal_timeout_sec = (
            self.stream_final_timeout_sec
            + (2 * self.worker_kill_grace_sec)
            + (6 * self.stream_transport_timeout_sec)
        )
        if terminal_timeout_sec > 120.0:
            raise ValueError(
                "stream terminal timeout budget must be <= 120 seconds "
                "(stream_final_timeout_sec + 2*worker_kill_grace_sec "
                "+ 6*stream_transport_timeout_sec)"
            )
        if (
            self.environment in {"staging", "production"}
            and self.stream_final_worker_backend != "process"
        ):
            raise ValueError("stream_final_worker_backend must be process in staging/production")
        if (
            self.environment in {"staging", "production"}
            and self.stream_live_worker_backend != "process"
        ):
            raise ValueError("stream_live_worker_backend must be process in staging/production")
        return self


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
