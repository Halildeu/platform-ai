"""GET /health — liveness + readiness."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Request, Response, status

from app import __version__
from app.core.config import Settings, get_settings
from app.models.schemas import HealthResponse
from app.services.analysis_delivery import AnalysisDeliveryRuntime
from app.services.analyze import get_service
from app.services.ready_event_consumer import ReadyEventConsumerRuntime

router = APIRouter()


@router.get("/health", response_model=HealthResponse, summary="Liveness + readiness")
async def health(
    request: Request,
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> HealthResponse:
    """`ok` when backend ready (mock always ready); `loading` for LLM stubs."""
    result, _ = await _health_response(request, settings)
    return result


@router.get("/ready", response_model=HealthResponse, summary="Dependency readiness")
async def ready(
    request: Request,
    response: Response,
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> HealthResponse:
    result, model_loaded = await _health_response(request, settings)
    dependencies_ready = (
        model_loaded
        and (result.analysis_delivery is None or result.analysis_delivery.ready)
        and (result.ready_consumer is None or result.ready_consumer.ready)
    )
    if not dependencies_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return result


async def _health_response(request: Request, settings: Settings) -> tuple[HealthResponse, bool]:
    service = get_service(settings)
    delivery: AnalysisDeliveryRuntime = request.app.state.analysis_delivery
    ready_consumer: ReadyEventConsumerRuntime = request.app.state.ready_consumer
    delivery_health = await delivery.health()
    ready_health = await ready_consumer.health()
    # The inventory check performs synchronous network I/O. Keep it off the
    # event loop that also delivers live analysis responses, and reuse this
    # exact readiness observation rather than probing a second time in /ready.
    model_loaded = await asyncio.to_thread(lambda: service.model_loaded)
    status_value = "ok" if model_loaded else "loading"
    if status_value == "ok" and (
        delivery_health.status == "degraded" or ready_health.status == "degraded"
    ):
        status_value = "degraded"
    return (
        HealthResponse(
            status=status_value,
            version=__version__,
            backend=settings.backend,
            model=settings.effective_model,
            redact_pii=settings.redact_pii,
            ollama_think=settings.ollama_think if settings.backend == "ollama" else None,
            analysis_delivery=delivery_health,
            ready_consumer=ready_health,
        ),
        model_loaded,
    )
