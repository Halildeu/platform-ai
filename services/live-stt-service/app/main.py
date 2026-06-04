"""FastAPI app entrypoint.

Run:
    uvicorn app.main:app --host 0.0.0.0 --port 8200 --log-level info
"""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from typing import Any, cast

from fastapi import FastAPI, Request
from starlette.responses import Response
from starlette.middleware.base import BaseHTTPMiddleware

from app import __version__
from app.api import health, metrics, transcribe
from app.core.config import get_settings

logger = logging.getLogger(__name__)


class CorrelationIdLogFilter(logging.Filter):
    """Ensure third-party log records can use our correlation_id formatter."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "correlation_id"):
            record.correlation_id = "-"
        return True


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Extract X-Correlation-Id from request headers.

    If absent, generate a UUID4 and attach it to the request state.
    Downstream handlers (transcribe, health) can access it via
    request.state.correlation_id. Logs should include it as
    `extra={"correlation_id": ...}`.
    """

    HEADER = "X-Correlation-Id"

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        corr_id = request.headers.get(self.HEADER)
        if not corr_id:
            corr_id = str(uuid.uuid4())
        request.state.correlation_id = corr_id
        response = cast(Response, await call_next(request))
        if corr_id:
            response.headers[self.HEADER] = corr_id
        return response


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s [%(correlation_id)s] %(message)s",
    )
    for handler in logging.getLogger().handlers:
        handler.addFilter(CorrelationIdLogFilter())
    logger.info(
        "live-stt-service starting",
        extra={
            "version": __version__,
            "model": settings.model_name,
            "device": settings.device,
            "compute_type": settings.compute_type,
            "correlation_id": "startup",
        },
    )
    yield
    logger.info("live-stt-service stopping", extra={"correlation_id": "shutdown"})


app = FastAPI(
    title="live-stt-service",
    version=__version__,
    description=(
        "Faz 24 Meeting Intelligence — Whisper synchronous transcribe (PoC). "
        "WebSocket streaming + diarization separate services."
    ),
    lifespan=lifespan,
)

app.add_middleware(CorrelationIdMiddleware)

app.include_router(health.router, tags=["health"])
app.include_router(metrics.router, tags=["metrics"])
app.include_router(transcribe.router, tags=["transcribe"])
