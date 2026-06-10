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

Faz 24 Plan (ADR-0030 + Codex 019e879c iter-3 absorb):
- correlation_id from X-Correlation-Id header (CorrelationIdMiddleware)
- meetingId / sessionId / deviceId from query params (Gateway contract)
- language: ISO 639-1 required, per-request override of Settings default
- All structured log entries include correlation_id for audit correlation
"""

from __future__ import annotations

import asyncio
import logging
import re
from io import BytesIO

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, status
from starlette.concurrency import run_in_threadpool

from app.api.metrics import (
    TranscribeResult,
    _normalise_format,
    stt_audio_bytes_total,
    stt_oom_total,
    stt_pii_redaction_total,
    stt_timeout_total,
    stt_transcribe_duration_seconds,
    stt_transcribe_total,
    stt_worker_killed_total,
)
from app.core.config import Settings, get_settings
from app.models.schemas import TranscribeResponse
from app.services.transcribe import TranscribeService, get_service
from app.services.worker import WorkerCrashedError, WorkerTimeoutError

router = APIRouter()
logger = logging.getLogger(__name__)

# --- correlation helpers ---

_CORR_ID_RE = re.compile(
    r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$",
    re.IGNORECASE,
)


def _correlation_id_from_request(request: Request) -> str:
    """Read correlation_id from request state (set by CorrelationIdMiddleware)."""
    return getattr(request.state, "correlation_id", "")


# --- PII redaction ---

# Subset of redaction patterns used in structured logs; never redact the
# structured log keys — only values that may contain raw token/email/path.
#
# Canonical patterns per observability-skeleton-meeting-intelligence.md
# (Codex rev 019e8846 absorb):
#   - bearer/secret/password/token → REDACTED
#   - email                       → REDACTED_EMAIL
#   - TC kimlik (11-digit, first ≠ 0) → REDACTED_TC
#   - IBAN TR                     → REDACTED_IBAN
#   - TR phone                    → REDACTED_PHONE
_REDACT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # bearer <jwt>: a dotted token directly after `bearer`. The dot is required
    # so this does NOT swallow `bearer token=<jwt>` (handled by the key=value
    # rule below, which preserves the `token=` prefix). Issue #97.
    (re.compile(r"(?i)bearer[\s:=]+[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_.]+"), "***REDACTED***"),
    # secret/password/token = value: keep the key + separator, redact only the
    # value (so `password=x` -> `password=***REDACTED***`). Issue #97.
    (re.compile(r"(?i)(secret|password|token)([\s:=]+)\S+"), r"\g<1>\g<2>***REDACTED***"),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "***REDACTED_EMAIL***"),
    # TC kimlik: any 11-digit run starting 1-9. Privacy-first — a bare 11-digit
    # number is redacted even without a `TC:` cue (#97 resolves the ambiguity).
    (re.compile(r"\b[1-9]\d{10}\b"), "***REDACTED_TC***"),
    # IBAN TR: TR prefix + 24 digits
    (re.compile(r"\bTR\d{24}\b"), "***REDACTED_IBAN***"),
    # Turkish mobile: optional +90/0 prefix with optional spaces/dashes, 5xx
    # group. No leading \b so a `+90 ...` prefix is captured. Issue #97.
    (
        re.compile(r"(?i)(?:\+90|0)?[\s-]?5\d{2}[\s-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}\b"),
        "***REDACTED_PHONE***",
    ),
]


def _redact_log_value(value: str) -> str:
    """Redact PII from a single log value string.

    Used only for structured log extra values — never for API response bodies.
    """
    result = str(value)
    for pattern, replacement in _REDACT_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def _log_extra(corr_id: str, **kwargs: object) -> dict[str, object]:
    """Build structured log extra dict with correlation_id and PII redaction."""
    extra: dict[str, object] = {"correlation_id": corr_id}
    for k, v in kwargs.items():
        if isinstance(v, str):
            redacted = _redact_log_value(v)
            if redacted != v:
                stt_pii_redaction_total.labels(pattern_class="log_value").inc()
            extra[k] = redacted
        else:
            extra[k] = v
    return extra


def _sanitize_error(exc: BaseException, correlation_id: str = "") -> str:
    """Return a non-PII error summary — exception class name only.

    Never echo `str(exc)` back to the caller; faster-whisper can embed
    audio paths, ffmpeg stderr fragments, or even partial transcript
    snippets in its exception messages.
    """
    logger.warning(
        "Transcribe error",
        extra=_log_extra(correlation_id, err_class=type(exc).__name__),
    )
    return type(exc).__name__


@router.post(
    "/transcribe",
    response_model=TranscribeResponse,
    status_code=status.HTTP_200_OK,
    summary="Whisper transcribe (sync, PoC)",
)
async def transcribe_endpoint(
    request: Request,
    audio: UploadFile,
    meeting_id: str | None = Query(
        default=None,
        max_length=64,
        description="Meeting identifier from Gateway",
    ),
    session_id: str | None = Query(
        default=None,
        max_length=64,
        description="Session identifier from Gateway",
    ),
    device_id: str | None = Query(
        default=None,
        max_length=64,
        description="Device identifier from Gateway",
    ),
    language: str | None = Query(
        default=None,
        max_length=10,
        description="ISO 639-1 language override (e.g. tr, en, de)",
    ),
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

    Streaming / chunked back-pressure = separate slice (out of PoC scope; Codex
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
    corr_id: str = _correlation_id_from_request(request)

    # Build metadata dict for structured logs (PII-safe: no raw tokens/paths)
    log_meta = {
        "correlation_id": corr_id,
        "meeting_id": (meeting_id or ""),
        "session_id": (session_id or ""),
        "device_id": (device_id or ""),
        "language_override": (language or ""),
        "size_mb": round(size_mb, 2),
    }

    try:
        if settings.worker_backend == "process":
            # Worker-level timeout kills and respawns the child process before
            # returning, avoiding the old wait_for/thread leak.
            result = await run_in_threadpool(
                service.transcribe,
                BytesIO(raw),
                settings.request_timeout,
            )
        else:
            # Inline backend is test/dev only; keep the outer async timeout for
            # patched in-process sleeps.
            result = await asyncio.wait_for(
                run_in_threadpool(service.transcribe, BytesIO(raw)),
                timeout=settings.request_timeout,
            )
    except WorkerTimeoutError as exc:
        logger.warning(
            "Transcribe timeout",
            extra=_log_extra(corr_id, timeout_sec=settings.request_timeout, **log_meta),
        )
        stt_timeout_total.labels(model=settings.model_name).inc()
        stt_worker_killed_total.labels(reason="timeout").inc()
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=(
                f"Transcribe exceeded {settings.request_timeout}s timeout "
                f"({_sanitize_error(exc, corr_id)})"
            ),
        ) from exc
    except (asyncio.TimeoutError, TimeoutError) as exc:  # noqa: UP041
        logger.warning(
            "Transcribe timeout",
            extra=_log_extra(corr_id, timeout_sec=settings.request_timeout, **log_meta),
        )
        stt_timeout_total.labels(model=settings.model_name).inc()
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=(
                f"Transcribe exceeded {settings.request_timeout}s timeout "
                f"({_sanitize_error(exc, corr_id)})"
            ),
        ) from exc
    except MemoryError as exc:
        logger.error(
            "Transcribe OOM",
            extra=_log_extra(
                corr_id,
                **log_meta,
                err_class=_sanitize_error(exc, corr_id),
            ),
        )
        stt_oom_total.inc()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Resource exhaustion (memory)",
        ) from exc
    except WorkerCrashedError as exc:
        logger.error(
            "Transcribe worker crashed",
            extra=_log_extra(
                corr_id,
                **log_meta,
                err_class=_sanitize_error(exc, corr_id),
            ),
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="STT worker crashed",
        ) from exc
    except (ValueError, RuntimeError) as exc:
        # faster-whisper raises RuntimeError on bad audio decode + inference,
        # ValueError on bad argument. Both are caller-input class problems.
        logger.warning(
            "Transcribe decode/inference failure",
            extra=_log_extra(
                corr_id,
                **log_meta,
                err_class=_sanitize_error(exc, corr_id),
            ),
        )
        stt_transcribe_total.labels(
            model=settings.model_name,
            language=settings.language,
            result=TranscribeResult.CLIENT_ERROR.value,
        ).inc()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Audio decode or inference failed ({_sanitize_error(exc, corr_id)})",
        ) from exc
    except OSError as exc:
        logger.error(
            "Transcribe I/O failure",
            extra=_log_extra(
                corr_id,
                **log_meta,
                err_class=_sanitize_error(exc, corr_id),
            ),
        )
        stt_transcribe_total.labels(
            model=settings.model_name,
            language=settings.language,
            result=TranscribeResult.IO_ERROR.value,
        ).inc()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"I/O failure ({_sanitize_error(exc, corr_id)})",
        ) from exc

    logger.info(
        "Transcribe success",
        extra=_log_extra(
            corr_id,
            **log_meta,
            duration_sec=result.duration,
            elapsed_ms=result.elapsed_ms,
            segments=len(result.segments),
            language=result.language,
        ),
    )

    # Prometheus metrics (PII-safe: language/labels only, no transcript/text)
    fmt = _normalise_format(audio.content_type)
    stt_transcribe_total.labels(
        model=settings.model_name,
        language=result.language,
        result=TranscribeResult.SUCCESS.value,
    ).inc()
    stt_transcribe_duration_seconds.labels(
        model=settings.model_name,
        language=result.language,
    ).observe(result.elapsed_ms / 1000.0)
    stt_audio_bytes_total.labels(format=fmt.value).inc(len(raw))

    return result
