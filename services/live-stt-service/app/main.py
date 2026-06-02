"""FastAPI app entrypoint.

Run:
    uvicorn app.main:app --host 0.0.0.0 --port 8200 --log-level info
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.api import health, transcribe
from app.core.config import get_settings

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger.info(
        "live-stt-service starting",
        extra={
            "version": __version__,
            "model": settings.model_name,
            "device": settings.device,
            "compute_type": settings.compute_type,
        },
    )
    yield
    logger.info("live-stt-service stopping")


app = FastAPI(
    title="live-stt-service",
    version=__version__,
    description=(
        "Faz 24 Meeting Intelligence — Whisper synchronous transcribe (PoC). "
        "WebSocket streaming + diarization separate services."
    ),
    lifespan=lifespan,
)

app.include_router(health.router, tags=["health"])
app.include_router(transcribe.router, tags=["transcribe"])
