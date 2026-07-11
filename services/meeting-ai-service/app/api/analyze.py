"""POST /analyze — transcript → summary + decisions + action items.

KVKK: the transcript is redacted in the service layer before any analyzer/LLM
call; raw transcript text is never logged (only lengths/counts/metadata).
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from starlette.concurrency import run_in_threadpool

from app.api.metrics import (
    AnalyzeResult,
    mai_analyze_duration_seconds,
    mai_analyze_total,
    mai_pii_redaction_total,
    mai_transcript_chars_total,
)
from app.core.config import Settings, get_settings
from app.models.schemas import AnalyzeRequest, AnalyzeResponse
from app.services.analysis_delivery import (
    AnalysisDeliveryContractError,
    AnalysisDeliveryRuntime,
)
from app.services.analyze import BackendUnavailableError, MeetingAnalysisService, get_service
from app.services.durable_outbox import OutboxError, OutboxFullError
from app.services.redact import RedactionError

router = APIRouter()
logger = logging.getLogger(__name__)


def _correlation_id(request: Request) -> str:
    return getattr(request.state, "correlation_id", "")


@router.post(
    "/analyze",
    response_model=AnalyzeResponse,
    status_code=status.HTTP_200_OK,
    summary="Summarize transcript (skeleton)",
)
async def analyze_endpoint(
    request: Request,
    response: Response,
    body: AnalyzeRequest,
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> AnalyzeResponse:
    """Error map: 400 empty, 413 too large, 501 LLM stub, 502 backend down, 504 timeout, 500 I/O."""
    transcript = body.transcript
    if len(transcript) > settings.max_transcript_chars:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Transcript {len(transcript)} chars > limit {settings.max_transcript_chars}",
        )

    service: MeetingAnalysisService = get_service(settings)
    corr_id = _correlation_id(request)
    log_extra = {
        "correlation_id": corr_id,
        "meeting_id": body.meeting_id or "",
        "session_id": body.session_id or "",
        "transcript_chars": len(transcript),
        "backend": settings.backend,
    }

    segments = [s.model_dump() for s in body.segments] if body.segments else None
    try:
        result = await asyncio.wait_for(
            run_in_threadpool(service.analyze, transcript, segments),
            timeout=settings.request_timeout,
        )
    except (asyncio.TimeoutError, TimeoutError) as exc:  # noqa: UP041
        logger.warning("Analyze timeout", extra=log_extra)
        mai_analyze_total.labels(backend=settings.backend, result=AnalyzeResult.TIMEOUT.value).inc()
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=f"Analyze exceeded {settings.request_timeout}s timeout",
        ) from exc
    except RedactionError as exc:
        # ADR-0043 D3 fail-closed: residual PII survived redaction → never reach the LLM.
        # Message is transcript-free (only detector labels).
        logger.warning(
            "Analyze blocked: residual PII after redaction (KVKK fail-closed)",
            extra={**log_extra, "err_class": type(exc).__name__},
        )
        mai_analyze_total.labels(
            backend=settings.backend, result=AnalyzeResult.REDACTION_BLOCKED.value
        ).inc()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Redaction could not guarantee PII removal; blocked ({exc})",
        ) from exc
    except NotImplementedError as exc:
        logger.warning("Analyze backend not implemented", extra=log_extra)
        mai_analyze_total.labels(
            backend=settings.backend, result=AnalyzeResult.NOT_IMPLEMENTED.value
        ).inc()
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Selected LLM backend is not wired yet",
        ) from exc
    except BackendUnavailableError as exc:
        logger.error(
            "Analyze backend unavailable",
            extra={**log_extra, "err_class": type(exc).__name__},
        )
        mai_analyze_total.labels(
            backend=settings.backend, result=AnalyzeResult.BACKEND_ERROR.value
        ).inc()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc
    except OSError as exc:
        logger.error(
            "Analyze I/O failure",
            extra={**log_extra, "err_class": type(exc).__name__},
        )
        mai_analyze_total.labels(
            backend=settings.backend, result=AnalyzeResult.IO_ERROR.value
        ).inc()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"I/O failure ({type(exc).__name__})",
        ) from exc

    logger.info(
        "Analyze success",
        extra={
            **log_extra,
            "elapsed_ms": result.elapsed_ms,
            "redaction_count": result.redaction_count,
            "decisions": len(result.decisions),
            "action_items": len(result.action_items),
        },
    )
    mai_analyze_duration_seconds.labels(backend=settings.backend).observe(
        result.elapsed_ms / 1000.0
    )
    mai_transcript_chars_total.labels(backend=settings.backend).inc(len(transcript))
    if result.redaction_count:
        mai_pii_redaction_total.labels(backend=settings.backend).inc(result.redaction_count)

    delivery: AnalysisDeliveryRuntime = request.app.state.analysis_delivery
    try:
        analysis_run_id = await delivery.enqueue_analysis(
            meeting_id=body.meeting_id,
            session_id=body.session_id,
            transcript=transcript,
            result=result,
        )
    except AnalysisDeliveryContractError as exc:
        logger.warning(
            "Durable analysis delivery contract rejected",
            extra={**log_extra, "err_class": type(exc).__name__},
        )
        mai_analyze_total.labels(
            backend=settings.backend, result=AnalyzeResult.CLIENT_ERROR.value
        ).inc()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except OutboxFullError as exc:
        logger.error("Durable analysis delivery queue full", extra=log_extra)
        mai_analyze_total.labels(
            backend=settings.backend, result=AnalyzeResult.DELIVERY_ERROR.value
        ).inc()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Durable analysis delivery queue is full; retry after operator recovery",
            headers={"Retry-After": "30"},
        ) from exc
    except OutboxError as exc:
        logger.error(
            "Durable analysis delivery enqueue failed",
            extra={**log_extra, "err_class": type(exc).__name__},
        )
        mai_analyze_total.labels(
            backend=settings.backend, result=AnalyzeResult.DELIVERY_ERROR.value
        ).inc()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Durable analysis delivery is unavailable",
            headers={"Retry-After": "5"},
        ) from exc
    if analysis_run_id is not None:
        response.headers["X-Analysis-Run-Id"] = analysis_run_id
        response.headers["X-Analysis-Delivery"] = "queued"

    mai_analyze_total.labels(backend=settings.backend, result=AnalyzeResult.SUCCESS.value).inc()

    return result
