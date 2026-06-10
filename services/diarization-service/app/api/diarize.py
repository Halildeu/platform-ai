"""POST /diarize — speaker diarization (skeleton, mock backend default).

KVKK discipline mirrors live-stt: raw audio is never logged, only metadata;
internal exception messages are sanitized to the class name; speaker labels are
anonymous (no voiceprint/biometric identity).
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, status
from starlette.concurrency import run_in_threadpool

from app.api.metrics import (
    DiarizeResult,
    dia_audio_bytes_total,
    dia_diarize_duration_seconds,
    dia_diarize_total,
    dia_speakers_detected,
)
from app.core.config import Settings, get_settings
from app.models.schemas import DiarizeResponse
from app.services.diarize import DiarizationService, get_service

router = APIRouter()
logger = logging.getLogger(__name__)


def _correlation_id(request: Request) -> str:
    return getattr(request.state, "correlation_id", "")


def _sanitize_error(exc: BaseException) -> str:
    """Return a non-PII error summary — exception class name only."""
    return type(exc).__name__


@router.post(
    "/diarize",
    response_model=DiarizeResponse,
    status_code=status.HTTP_200_OK,
    summary="Speaker diarization (skeleton)",
)
async def diarize_endpoint(
    request: Request,
    audio: UploadFile,
    meeting_id: str | None = Query(default=None, max_length=64),
    session_id: str | None = Query(default=None, max_length=64),
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> DiarizeResponse:
    """Accept multipart audio, return anonymous speaker segments.

    Error map: 400 bad/empty input, 413 too large, 501 pyannote stub,
    504 timeout, 500 unexpected I/O.
    """
    if audio.content_type and not audio.content_type.startswith(("audio/", "video/")):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported content_type: {audio.content_type}",
        )

    raw = await audio.read()
    size_mb = len(raw) / (1024 * 1024)
    if size_mb > settings.max_audio_mb:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Audio {size_mb:.1f} MB > limit {settings.max_audio_mb} MB",
        )
    if not raw:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty audio body")

    service: DiarizationService = get_service(settings)
    corr_id = _correlation_id(request)
    log_extra = {
        "correlation_id": corr_id,
        "meeting_id": meeting_id or "",
        "session_id": session_id or "",
        "size_mb": round(size_mb, 2),
        "backend": settings.backend,
    }

    try:
        result = await asyncio.wait_for(
            run_in_threadpool(service.diarize, raw),
            timeout=settings.request_timeout,
        )
    except (asyncio.TimeoutError, TimeoutError) as exc:  # noqa: UP041
        logger.warning("Diarize timeout", extra=log_extra)
        dia_diarize_total.labels(backend=settings.backend, result=DiarizeResult.TIMEOUT.value).inc()
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=f"Diarize exceeded {settings.request_timeout}s timeout",
        ) from exc
    except NotImplementedError as exc:
        logger.warning("Diarize backend not implemented", extra=log_extra)
        dia_diarize_total.labels(
            backend=settings.backend, result=DiarizeResult.NOT_IMPLEMENTED.value
        ).inc()
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Selected diarization backend is not wired yet",
        ) from exc
    except ValueError as exc:
        logger.warning(
            "Diarize bad input",
            extra={**log_extra, "err_class": _sanitize_error(exc)},
        )
        dia_diarize_total.labels(
            backend=settings.backend, result=DiarizeResult.CLIENT_ERROR.value
        ).inc()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Audio decode failed ({_sanitize_error(exc)})",
        ) from exc
    except OSError as exc:
        logger.error(
            "Diarize I/O failure",
            extra={**log_extra, "err_class": _sanitize_error(exc)},
        )
        dia_diarize_total.labels(
            backend=settings.backend, result=DiarizeResult.IO_ERROR.value
        ).inc()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"I/O failure ({_sanitize_error(exc)})",
        ) from exc

    logger.info(
        "Diarize success",
        extra={
            **log_extra,
            "duration_sec": result.duration,
            "elapsed_ms": result.elapsed_ms,
            "num_speakers": result.num_speakers,
            "segments": len(result.segments),
        },
    )
    dia_diarize_total.labels(backend=settings.backend, result=DiarizeResult.SUCCESS.value).inc()
    dia_diarize_duration_seconds.labels(backend=settings.backend).observe(
        result.elapsed_ms / 1000.0
    )
    dia_speakers_detected.labels(backend=settings.backend).observe(result.num_speakers)
    dia_audio_bytes_total.labels(backend=settings.backend).inc(len(raw))

    return result
