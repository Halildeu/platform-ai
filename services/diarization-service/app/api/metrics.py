"""Prometheus metrics for diarization-service.

Exposes `/metrics`. Names follow the canonical `dia_*` / `kvkk_*` namespace.
Labels never carry raw PII (no meeting/user identifiers, no audio content).
"""

from __future__ import annotations

from enum import Enum

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest


class DiarizeResult(str, Enum):
    SUCCESS = "success"  # 200
    CLIENT_ERROR = "client_error"  # 400 / 413
    IO_ERROR = "io_error"  # 500
    TIMEOUT = "timeout"  # 504
    NOT_IMPLEMENTED = "not_implemented"  # 501 (pyannote stub)


router = APIRouter()

dia_diarize_total = Counter(
    "dia_diarize_total",
    "Total diarize calls",
    ["backend", "result"],
)

dia_diarize_duration_seconds = Histogram(
    "dia_diarize_duration_seconds",
    "Diarization wall-clock",
    ["backend"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

dia_speakers_detected = Histogram(
    "dia_speakers_detected",
    "Distinct speakers detected per request",
    ["backend"],
    buckets=(1, 2, 3, 4, 5, 6, 8, 10),
)

dia_audio_bytes_total = Counter(
    "dia_audio_bytes_total",
    "Total audio bytes received",
    ["backend"],
)

dia_pii_redaction_total = Counter(
    "dia_pii_redaction_total",
    "PII redaction events in logs",
    ["pattern_class"],
)

kvkk_audit_event_total = Counter(
    "kvkk_audit_event_total",
    "KVKK audit events",
    ["action", "result"],
)


@router.get("/metrics", summary="Prometheus metrics endpoint")
def metrics() -> Response:
    """Expose Prometheus metrics for Grafana scraping."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
