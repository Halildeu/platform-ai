"""GET /health — liveness + readiness."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app import __version__
from app.core.config import Settings, get_settings
from app.models.schemas import HealthResponse
from app.services.analysis_delivery import AnalysisDeliveryRuntime
from app.services.analyze import get_service

router = APIRouter()


@router.get("/health", response_model=HealthResponse, summary="Liveness + readiness")
async def health(
    request: Request,
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> HealthResponse:
    """`ok` when backend ready (mock always ready); `loading` for LLM stubs."""
    service = get_service(settings)
    delivery: AnalysisDeliveryRuntime = request.app.state.analysis_delivery
    delivery_health = await delivery.health()
    status_value = "ok" if service.model_loaded else "loading"
    if status_value == "ok" and delivery_health.status == "degraded":
        status_value = "degraded"
    return HealthResponse(
        status=status_value,
        version=__version__,
        backend=settings.backend,
        model=settings.effective_model,
        redact_pii=settings.redact_pii,
        analysis_delivery=delivery_health,
    )
