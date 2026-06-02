"""POST /transcribe — synchronous Whisper transcribe endpoint (PoC)."""

from __future__ import annotations

import logging
from io import BytesIO

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status

from app.core.config import Settings, get_settings
from app.models.schemas import TranscribeResponse
from app.services.transcribe import TranscribeService, get_service

router = APIRouter()
logger = logging.getLogger(__name__)


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
      - Max body size enforced via STT_MAX_AUDIO_MB
      - Returns full text + segments + meta
      - 413 if oversize, 400 if format unreadable, 500 on Whisper error

    Streaming + WebSocket = ayrı slice (PoC scope dışı).
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
        result = service.transcribe(BytesIO(raw))
    except RuntimeError as exc:  # faster-whisper raises generic RuntimeError on bad audio
        logger.warning("Transcribe runtime error", extra={"err": str(exc)})
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Audio decode/inference failed: {exc}",
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
