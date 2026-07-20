"""Prometheus metrics for meeting-ai-service.

Names follow the canonical `mai_*` / `kvkk_*` namespace. Labels never carry raw
PII or transcript content.
"""

from __future__ import annotations

from enum import Enum

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest


class AnalyzeResult(str, Enum):
    SUCCESS = "success"  # 200
    CLIENT_ERROR = "client_error"  # 400 / 413
    IO_ERROR = "io_error"  # 500
    TIMEOUT = "timeout"  # 504
    NOT_IMPLEMENTED = "not_implemented"  # 501 (LLM stub)
    BACKEND_ERROR = "backend_error"  # 502 (LLM backend unreachable/unusable)
    REDACTION_BLOCKED = "redaction_blocked"  # 422 (ADR-0043 D3 fail-closed residual PII)
    DELIVERY_ERROR = "delivery_error"  # 503 (durable system-of-record handoff unavailable)


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

mai_ingestion_enqueue_total = Counter(
    "mai_ingestion_enqueue_total",
    "Durable analysis-result enqueue attempts",
    ["outcome"],
)

mai_ingestion_delivery_total = Counter(
    "mai_ingestion_delivery_total",
    "Analysis-result delivery attempts",
    ["outcome"],
)

mai_ingestion_queue_depth = Gauge(
    "mai_ingestion_queue_depth",
    "Durable analysis-result rows by state",
    ["state"],
)

mai_ingestion_oldest_pending_age_seconds = Gauge(
    "mai_ingestion_oldest_pending_age_seconds",
    "Age of the oldest pending or leased analysis-result row",
)

# ── Faz 24 live-stream SSE relay (Zeynep 2026-07-20 kapsam) ────────────
# Meeting-id label deliberately absent — cardinality is unbounded (UUIDs) and
# a per-meeting breakdown belongs in traces, not metrics. Aggregate signals
# are enough for capacity + drop alerting.
mai_analyze_live_stream_subscribers = Gauge(
    "mai_analyze_live_stream_subscribers",
    "Currently connected SSE subscribers to /analyze/live/stream (all meetings)",
)

mai_analyze_live_stream_delivered_total = Counter(
    "mai_analyze_live_stream_delivered_total",
    "Live analysis events successfully enqueued to a subscriber",
)

mai_analyze_live_stream_dropped_total = Counter(
    "mai_analyze_live_stream_dropped_total",
    "Live analysis events dropped due to a full subscriber queue (drop-oldest)",
)

mai_analyze_live_stream_published_total = Counter(
    "mai_analyze_live_stream_published_total",
    "Publish attempts to the live-stream hub (0-subscriber calls counted here too)",
)


@router.get("/metrics", summary="Prometheus metrics endpoint")
def metrics() -> Response:
    """Expose Prometheus metrics for Grafana scraping."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
