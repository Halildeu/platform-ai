"""Liveness and readiness endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app import __version__
from app.core.config import Settings, get_settings
from app.models.schemas import HealthResponse
from app.services.transcribe import get_transcriber

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
def health(settings: Settings = Depends(get_settings)) -> HealthResponse:  # noqa: B008
    transcriber = get_transcriber(settings)
    return HealthResponse(
        status="ok" if transcriber.model_loaded else "loading",
        version=__version__,
        model=settings.model_name,
        model_revision=settings.model_revision,
        device=settings.device,
        compute_type=settings.compute_type,
        redis_enabled=settings.redis_enabled,
    )
