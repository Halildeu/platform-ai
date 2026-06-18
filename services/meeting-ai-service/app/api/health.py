"""GET /health — liveness + readiness."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app import __version__
from app.core.config import Settings, get_settings
from app.models.schemas import HealthResponse
from app.services.analyze import get_service

router = APIRouter()


@router.get("/health", response_model=HealthResponse, summary="Liveness + readiness")
async def health(
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> HealthResponse:
    """`ok` when backend ready (mock always ready); `loading` for LLM stubs."""
    service = get_service(settings)
    return HealthResponse(
        status="ok" if service.model_loaded else "loading",
        version=__version__,
        backend=settings.backend,
        model=settings.effective_model,
        redact_pii=settings.redact_pii,
    )
