"""POST /transcribe — synchronous Whisper transcribe endpoint (PoC).

Codex `019e877b` iter-1 REVISE absorb:
- Blocking Whisper inference moved to threadpool (`run_in_threadpool`) so
  the event loop is not parked while CPU model runs. Single worker is fine
  for PoC.
- `STT_REQUEST_TIMEOUT` is now enforced via `asyncio.wait_for`; previously
  it was advertised but never applied. Timeout → 504.
- Error mapping refined: distinct paths for input/decode (400), timeout (504),
  resource exhaustion (503), I/O (500). Internal exception messages are
  sanitized — only exception class name is surfaced, never the raw `str(exc)`
  that may carry PII or audio path leaks.
"""

from __future__ import annotations

import asyncio
import logging
from io import BytesIO

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from starlette.concurrency import run_in_threadpool

from app.core.config import Settings, get_settings
from app.models.schemas import TranscribeResponse
from app.services.transcribe import TranscribeService, get_service

router = APIRouter()
logger = logging.getLogger(__name__)


def _sanitize_error(exc: BaseException) -> str:
    """Return a non-PII error summary — exception class name only.

    Never echo `str(exc)` back to the caller; faster-whisper can embed
    audio paths, ffmpeg stderr fragments, or even partial transcript
    snippets in its exception messages.
    """
    return type(exc).__name__


@router.post(
    "/transcribe",
    response_model=TranscribeResponse,
    status_code=status.HTTP_200_OK,
    summary="Whisper transcribe (sync, PoC)",
)
async def transcribe_endpoint(
    audio: UploadFile,
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> TranscribeResponse:
    """Synchronous transcribe.

    PoC contract:
      - Accepts multipart audio file (wav/mp3/m4a/ogg/flac — anything ffmpeg reads)
      - Max body size enforced via `STT_MAX_AUDIO_MB`
      - Inference runs in threadpool (event loop never blocked)
      - Total wall-clock capped by `STT_REQUEST_TIMEOUT`
      - Returns full text + segments + meta

    Error map:
      - 400 unsupported content-type / empty body / oversized / decode fail
      - 413 audio > `STT_MAX_AUDIO_MB`
      - 503 model load OOM / resource exhaustion
      - 504 inference exceeded `STT_REQUEST_TIMEOUT`
      - 500 unexpected I/O

    Streaming / chunked back-pressure = ayrı slice (PoC scope dışı; Codex
    `019e877b` notu).
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
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Empty audio body"
        )

    service: TranscribeService = get_service(settings)

    try:
        # Inference is CPU-bound and blocking. Push to threadpool so the
        # event loop continues to serve /health and other requests.
        result = await asyncio.wait_for(
            run_in_threadpool(service.transcribe, BytesIO(raw)),
            timeout=settings.request_timeout,
        )
    except asyncio.TimeoutError as exc:
        logger.warning(
            "Transcribe timeout",
            extra={"timeout_sec": settings.request_timeout, "size_mb": round(size_mb, 2)},
        )
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=(
                f"Transcribe exceeded {settings.request_timeout}s timeout "
                f"({_sanitize_error(exc)})"
            ),
        ) from exc
    except MemoryError as exc:
        logger.error("Transcribe OOM", extra={"err_class": _sanitize_error(exc)})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Resource exhaustion (memory)",
        ) from exc
    except (ValueError, RuntimeError) as exc:
        # faster-whisper raises RuntimeError on bad audio decode + inference,
        # ValueError on bad argument. Both are caller-input class problems.
        logger.warning(
            "Transcribe decode/inference failure",
            extra={"err_class": _sanitize_error(exc), "size_mb": round(size_mb, 2)},
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Audio decode or inference failed ({_sanitize_error(exc)})",
        ) from exc
    except OSError as exc:
        logger.error("Transcribe I/O failure", extra={"err_class": _sanitize_error(exc)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"I/O failure ({_sanitize_error(exc)})",
        ) from exc

    logger.info(
        "Transcribe success",
        extra={
            "duration_sec": result.duration,
            "elapsed_ms": result.elapsed_ms,
            "segments": len(result.segments),
            "language": result.language,
        },
    )
    return result
