"""Liveness and explicit streaming-model readiness endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app import __version__
from app.core.config import Settings, get_settings
from app.models.schemas import HealthResponse
from app.services.model_preload import StreamingPreloadState
from app.services.streaming_models import streaming_services_healthy
from app.services.transcribe import get_service

router = APIRouter()


def _preload_state(request: Request, settings: Settings) -> StreamingPreloadState:
    state = getattr(request.app.state, "streaming_preload", None)
    if isinstance(state, StreamingPreloadState):
        return state
    return StreamingPreloadState(enabled=settings.stream_preload_models)


@router.get("/health", response_model=HealthResponse, summary="Process liveness")
async def health(
    request: Request,
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> HealthResponse:
    """Status:
    - `loading`  → process is up but the legacy sync model is still lazy
    - `ok`       → legacy sync model is loaded
    - `degraded` → streaming preload failed or a supervised worker is unhealthy

    Traffic admission for the customer streaming path uses `/ready`.
    """
    service = get_service(settings)
    status = "ok" if service.model_loaded else "loading"
    preload = _preload_state(request, settings).snapshot()
    if not streaming_services_healthy() or preload.status == "failed":
        status = "degraded"
    return HealthResponse(
        status=status,
        version=__version__,
        model=settings.model_name,
        model_revision=settings.model_revision,
        model_sha256=settings.model_sha256,
        device=settings.device,
        compute_type=settings.compute_type,
    )


@router.get("/ready", summary="Streaming model readiness")
async def ready(
    request: Request,
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> JSONResponse:
    """Fail closed until both configured streaming models are loaded."""
    snapshot = _preload_state(request, settings).snapshot()
    healthy = streaming_services_healthy()
    is_ready = snapshot.ready and healthy
    effective_status: str = snapshot.status
    if snapshot.ready and not healthy:
        effective_status = "unhealthy"
    return JSONResponse(
        status_code=200 if is_ready else 503,
        content={
            "status": "ready" if is_ready else effective_status,
            "runtime_commit": settings.runtime_commit,
            "preload_budget_sec": settings.stream_preload_readiness_budget_sec,
            "streaming_preload_enabled": snapshot.enabled,
            "roles": snapshot.roles,
            "attempts": snapshot.attempts,
            "workers_healthy": healthy,
            "runtime": {
                "legacy": {"device": settings.device, "compute_type": settings.compute_type},
                "live": {
                    "device": settings.live_device,
                    "compute_type": settings.live_compute_type,
                },
                "final": {
                    "device": settings.final_device,
                    "compute_type": settings.final_compute_type,
                },
            },
            "speech_gate": {
                "profile": settings.speech_gate_profile,
                "rms_source": settings.speech_gate_rms_source,
                "silence_rms": settings.silence_rms,
                "min_speech_rms": settings.min_speech_rms,
                "live_infer_interval_ms": settings.live_infer_interval_ms,
                "live_window_sec": settings.live_window_sec,
                "final_window_sec": settings.final_window_sec,
                "forced_commit_sec": settings.forced_commit_sec,
                "silence_commit_sec": settings.silence_commit_sec,
                "tail_overlap_sec": settings.tail_overlap_sec,
                "min_infer_sec": settings.min_infer_sec,
                "vad": {
                    "live_enabled": settings.stream_live_vad_filter,
                    "final_enabled": settings.stream_final_vad_filter,
                    "threshold": settings.stream_vad_threshold,
                    "min_speech_duration_ms": settings.stream_vad_min_speech_duration_ms,
                    "min_silence_duration_ms": settings.stream_vad_min_silence_duration_ms,
                    "speech_pad_ms": settings.stream_vad_speech_pad_ms,
                },
            },
        },
    )
