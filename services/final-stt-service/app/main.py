"""FastAPI entrypoint and optional Redis consumer lifecycle."""

from __future__ import annotations

import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from app import __version__
from app.api import health, metrics
from app.core.config import get_settings
from app.services.consumer import FinalSttConsumer, build_redis_client
from app.services.transcribe import get_transcriber

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(message)s",
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
    )
    logger.info(
        "final_stt_service_starting",
        model=settings.model_name,
        device=settings.device,
        compute_type=settings.compute_type,
    )

    consumer: FinalSttConsumer | None = None
    consumer_thread: threading.Thread | None = None
    if settings.redis_enabled:
        consumer = FinalSttConsumer(
            settings,
            get_transcriber(settings),
            build_redis_client(settings),
        )
        consumer_thread = threading.Thread(
            target=consumer.run,
            name="final-stt-redis-consumer",
            daemon=True,
        )
        consumer_thread.start()
    app.state.consumer = consumer
    yield
    if consumer is not None:
        consumer.stop()
    if consumer_thread is not None:
        consumer_thread.join(timeout=(settings.redis_block_ms / 1000) + 1)
    logger.info("final_stt_service_stopping")


app = FastAPI(
    title="final-stt-service",
    version=__version__,
    description="Contextual final transcript compute worker",
    lifespan=lifespan,
)
app.include_router(health.router, tags=["health"])
app.include_router(metrics.router, tags=["metrics"])
