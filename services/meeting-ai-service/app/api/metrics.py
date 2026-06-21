"""Prometheus metrics for meeting-ai-service.

Names follow the canonical `mai_*` / `kvkk_*` namespace. Labels never carry raw
PII or transcript content.
"""

from __future__ import annotations

from enum import Enum

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest


class AnalyzeResult(str, Enum):
    SUCCESS = "success"  # 200
    CLIENT_ERROR = "client_error"  # 400 / 413
    IO_ERROR = "io_error"  # 500
    TIMEOUT = "timeout"  # 504
    NOT_IMPLEMENTED = "not_implemented"  # 501 (LLM stub)
    BACKEND_ERROR = "backend_error"  # 502 (LLM backend unreachable/unusable)
    REDACTION_BLOCKED = "redaction_blocked"  # 422 (ADR-0043 D3 fail-closed residual PII)


router = APIRouter()

mai_analyze_total = Counter(
    "mai_analyze_total",
    "Total analyze calls",
    ["backend", "result"],
)

mai_analyze_duration_seconds = Histogram(
    "mai_analyze_duration_seconds",
    "Analysis wall-clock",
    ["backend"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

mai_transcript_chars_total = Counter(
    "mai_transcript_chars_total",
    "Total transcript characters received",
    ["backend"],
)

mai_pii_redaction_total = Counter(
    "mai_pii_redaction_total",
    "PII spans redacted before analysis",
    ["backend"],
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
