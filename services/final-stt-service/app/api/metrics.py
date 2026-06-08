"""Prometheus metrics without transcript or identifier labels."""

from __future__ import annotations

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

router = APIRouter()

final_stt_jobs_total = Counter(
    "final_stt_jobs_total",
    "Final STT jobs by bounded result",
    ["result"],
)
final_stt_inference_seconds = Histogram(
    "final_stt_inference_seconds",
    "Final STT inference wall clock",
    buckets=(0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 30.0, 60.0),
)
final_stt_consumer_up = Gauge(
    "final_stt_consumer_up",
    "Whether the Redis consumer loop is running",
)
final_stt_chunk_duration_seconds = Histogram(
    "final_stt_chunk_duration_seconds",
    "Declared final STT audio chunk duration",
    buckets=(5.0, 10.0, 12.5, 15.0, 20.0),
)
final_stt_revision_events_total = Counter(
    "final_stt_revision_events_total",
    "Final STT revision events by bounded state",
    ["state"],
)


@router.get("/metrics")
def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
