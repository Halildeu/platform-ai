"""GET /health — liveness + readiness."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app import __version__
from app.core.config import Settings, get_settings
from app.models.schemas import HealthResponse
from app.services.transcribe import get_service

router = APIRouter()


@router.get("/health", response_model=HealthResponse, summary="Liveness + readiness")
async def health(
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> HealthResponse:
    """Status:
    - `loading`  → service up but model not yet loaded (first request pays cost)
    - `ok`       → model loaded
    - `degraded` → reserved for future failure-mode signaling
    """
    service = get_service(settings)
    return HealthResponse(
        status="ok" if service.model_loaded else "loading",
        version=__version__,
        model=settings.model_name,
        device=settings.device,
        compute_type=settings.compute_type,
    )
