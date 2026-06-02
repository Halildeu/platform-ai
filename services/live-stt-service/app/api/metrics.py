"""Prometheus metrics for live-stt-service.

Exposes `/metrics` endpoint (Grafana-compatible). Metric names follow
canonical `stt_*` / `meeting_*` / `kvkk_*` namespace per
platform-k8s-gitops docs/observability-skeleton-meeting-intelligence.md.

All labels use hashed or bucketed values — never raw PII
(meeting_id → meeting_id_hash, user_id → user_hash_prefix(8)).
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST

from fastapi import APIRouter, Response

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