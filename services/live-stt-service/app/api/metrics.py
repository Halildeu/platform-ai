"""Prometheus metrics for live-stt-service.

Exposes `/metrics` endpoint (Grafana-compatible). Metric names follow
canonical `stt_*` / `meeting_*` / `kvkk_*` namespace per
platform-k8s-gitops docs/observability-skeleton-meeting-intelligence.md.

All labels use hashed or bucketed values — never raw PII
(meeting_id → meeting_id_hash, user_id → user_hash_prefix(8)).
"""

from __future__ import annotations

from enum import Enum

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

# ── Result enum (canonical set — Codex rev 019e8846) ──────────────────────────


class TranscribeResult(str, Enum):
    SUCCESS = "success"  # 200
    CLIENT_ERROR = "client_error"  # 400 / 413
    IO_ERROR = "io_error"  # 500
    TIMEOUT = "timeout"  # 504
    OOM = "oom"  # 503


# ── Normalised audio format enum (fixed set, cardinality-safe) ─────────────────


class AudioFormat(str, Enum):
    WAV = "wav"
    WEBM_OPUS = "webm-opus"
    PCM16 = "pcm16"
    MP3 = "mp3"
    M4A = "m4a"
    OGG = "ogg"
    FLAC = "flac"
    OTHER = "other"  # catch-all for unknown / unrecognised


def _normalise_format(content_type: str | None) -> AudioFormat:
    """Map raw Content-Type to fixed AudioFormat bucket."""
    if content_type is None:
        return AudioFormat.OTHER
    ct = content_type.lower()
    if "wav" in ct:
        return AudioFormat.WAV
    if "webm" in ct and "opus" in ct:
        return AudioFormat.WEBM_OPUS
    if "mp3" in ct or "mpeg" in ct:
        return AudioFormat.MP3
    if "m4a" in ct or "mp4" in ct:
        return AudioFormat.M4A
    if "ogg" in ct:
        return AudioFormat.OGG
    if "flac" in ct:
        return AudioFormat.FLAC
    if "pcm" in ct or "16bit" in ct or "raw" in ct:
        return AudioFormat.PCM16
    return AudioFormat.OTHER


router = APIRouter()

# --- STT metrics ---

stt_transcribe_total = Counter(
    "stt_transcribe_total",
    "Total transcribe calls",
    ["model", "language", "result"],
)

stt_transcribe_duration_seconds = Histogram(
    "stt_transcribe_duration_seconds",
    "Whisper inference wall-clock",
    ["model", "language"],
    buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
)

stt_audio_bytes_total = Counter(
    "stt_audio_bytes_total",
    "Total audio bytes received",
    ["format"],
)

stt_model_load_duration_seconds = Histogram(
    "stt_model_load_duration_seconds",
    "Lazy model load duration",
    ["model", "device"],
    buckets=(0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

stt_threadpool_active_workers = Gauge(
    "stt_threadpool_active_workers",
    "Active Whisper worker threads",
)

stt_timeout_total = Counter(
    "stt_timeout_total",
    "Transcribe 504 timeout count",
    ["model"],
)

stt_worker_killed_total = Counter(
    "stt_worker_killed_total",
    "STT worker processes killed and respawned",
    ["reason"],
)

stt_oom_total = Counter(
    "stt_oom_total",
    "Transcribe 503 OOM count",
)

stt_pii_redaction_total = Counter(
    "stt_pii_redaction_total",
    "PII redaction events in logs",
    ["pattern_class"],
)

# --- KVKK audit ---

kvkk_audit_event_total = Counter(
    "kvkk_audit_event_total",
    "KVKK audit events",
    ["action", "result"],
)

kvkk_consent_total = Counter(
    "kvkk_consent_total",
    "Consent granted/revoked events",
    ["granted_revoked"],
)


@router.get("/metrics", summary="Prometheus metrics endpoint")
def metrics() -> Response:
    """Expose Prometheus metrics for Grafana scraping."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
