"""FastAPI app entrypoint.

Run:
    uvicorn app.main:app --host 0.0.0.0 --port 8400 --log-level info
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from app import __version__
from app.api import analyze, ask, health, metrics
from app.core.config import get_settings

logger = logging.getLogger(__name__)


class CorrelationIdLogFilter(logging.Filter):
    """Ensure third-party log records can use our correlation_id formatter."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "correlation_id"):
            record.correlation_id = "-"
        return True


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Extract X-Correlation-Id from request headers; generate UUID4 if absent."""

    HEADER = "X-Correlation-Id"

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        corr_id = request.headers.get(self.HEADER) or str(uuid.uuid4())
        request.state.correlation_id = corr_id
        response = cast(Response, await call_next(request))
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
        "meeting-ai-service starting",
        extra={
            "version": __version__,
            "backend": settings.backend,
            "model": settings.effective_model,
            "redact_pii": settings.redact_pii,
            "correlation_id": "startup",
        },
    )
    yield
    logger.info("meeting-ai-service stopping", extra={"correlation_id": "shutdown"})


app = FastAPI(
    title="meeting-ai-service",
    version=__version__,
    description=(
        "Faz 24 Meeting Intelligence — transcript summary/decisions/actions "
        "(skeleton). Mock backend by default; real LLM (Anthropic/OpenAI/Ollama) "
        "is a follow-up. PII is redacted before any analyzer/LLM call."
    ),
    lifespan=lifespan,
)

app.add_middleware(CorrelationIdMiddleware)

app.include_router(health.router, tags=["health"])
app.include_router(metrics.router, tags=["metrics"])
app.include_router(analyze.router, tags=["analyze"])
app.include_router(ask.router, tags=["ask"])
