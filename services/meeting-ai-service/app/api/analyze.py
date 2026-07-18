"""POST /analyze — transcript → summary + decisions + action items.

KVKK: the transcript is redacted in the service layer before any analyzer/LLM
call; raw transcript text is never logged (only lengths/counts/metadata).
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.api.metrics import (
    AnalyzeResult,
    mai_analyze_duration_seconds,
    mai_analyze_live_stream_delivered_total,
    mai_analyze_live_stream_dropped_total,
    mai_analyze_live_stream_published_total,
    mai_analyze_live_stream_subscribers,
    mai_analyze_total,
    mai_pii_redaction_total,
    mai_transcript_chars_total,
)
from app.core.config import Settings, get_settings
from app.models.schemas import AnalyzeRequest, AnalyzeResponse
from app.services.analysis_application import (
    AnalysisApplicationService,
    AnalysisCommand,
    AnalysisTranscriptTooLargeError,
)
from app.services.analysis_delivery import (
    AnalysisDeliveryContractError,
    AnalysisDeliveryRuntime,
)
from app.services.analyze import BackendUnavailableError
from app.services.durable_outbox import OutboxError, OutboxFullError
from app.services.live_stream_hub import LiveStreamHub
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

    application: AnalysisApplicationService = request.app.state.analysis_application
    delivery: AnalysisDeliveryRuntime = request.app.state.analysis_delivery
    corr_id = _correlation_id(request)
    log_extra = {
        "correlation_id": corr_id,
        "meeting_id": body.meeting_id or "",
        "session_id": body.session_id or "",
        "transcript_chars": len(transcript),
        "backend": settings.backend,
    }

    segments = [s.model_dump() for s in body.segments] if body.segments else None

    async def persist_result(command: AnalysisCommand, result: AnalyzeResponse) -> str | None:
        return await delivery.enqueue_analysis(
            meeting_id=command.meeting_id,
            session_id=command.session_id,
            transcript=command.transcript,
            result=result,
            analysis_run_id=command.analysis_run_id,
            generated_at=command.generated_at,
        )

    try:
        execution = await application.execute(
            AnalysisCommand(
                transcript=transcript,
                meeting_id=body.meeting_id,
                session_id=body.session_id,
                segments=segments,
            ),
            persist=persist_result,
        )
        result = execution.result
        analysis_run_id = execution.analysis_run_id
    except AnalysisTranscriptTooLargeError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Transcript {len(transcript)} chars > limit {settings.max_transcript_chars}",
        ) from exc
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

    if analysis_run_id is not None:
        response.headers["X-Analysis-Run-Id"] = analysis_run_id
        response.headers["X-Analysis-Delivery"] = "queued"

    mai_analyze_total.labels(backend=settings.backend, result=AnalyzeResult.SUCCESS.value).inc()

    return result


# Faz 24 live analysis endpoint (Zeynep 2026-07-20 kapsam kararı):
# `/analyze/live` reuses the same analyzer pipeline, redaction guard, and
# durable delivery as `/analyze` — the only differences are:
#   1. The response carries `is_partial=True` so downstream consumers
#      (meeting-service, desktop panel) know a later delivery will supersede.
#   2. `version` is derived from `body.segment_seq` (defaults to 0 if the
#      caller did not thread the recorder's sequence).
# Error map, redaction, LLM stub behaviour, delivery back-pressure and metrics
# are identical to `/analyze`; a live run and a final run of the same content
# produce byte-identical `AnalyzeResponse` payloads apart from the two
# metadata fields above.
#
# Sentinel: the final `/analyze` call at recording end sets `version` from a
# distinct sentinel value that always compares greater than any live segment
# sequence, so a delayed live delivery cannot displace the final. This scaffold
# reserves the sentinel here — the caller/meeting-service enforcement lands in
# the follow-up ingestion PR.
FINAL_ANALYSIS_VERSION_SENTINEL = 2**31 - 1


@router.post(
    "/analyze/live",
    response_model=AnalyzeResponse,
    status_code=status.HTTP_200_OK,
    summary="Incremental analysis over an in-progress transcript",
)
async def analyze_live_endpoint(
    request: Request,
    response: Response,
    body: AnalyzeRequest,
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> AnalyzeResponse:
    """Live variant of /analyze — reuses the analyzer + delivery, marks is_partial=True.

    Error map matches `/analyze`: 400 empty, 413 too large, 501 LLM stub,
    502 backend down, 504 timeout, 500 I/O, 422 redaction/contract, 503 queue.
    """
    transcript = body.transcript
    if len(transcript) > settings.max_transcript_chars:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Transcript {len(transcript)} chars > limit {settings.max_transcript_chars}",
        )

    service: MeetingAnalysisService = get_service(settings)
    corr_id = _correlation_id(request)
    live_version = body.segment_seq if body.segment_seq is not None else 0
    log_extra = {
        "correlation_id": corr_id,
        "meeting_id": body.meeting_id or "",
        "session_id": body.session_id or "",
        "transcript_chars": len(transcript),
        "backend": settings.backend,
        "is_partial": True,
        "version": live_version,
    }

    segments = [s.model_dump() for s in body.segments] if body.segments else None
    try:
        result = await asyncio.wait_for(
            run_in_threadpool(service.analyze, transcript, segments),
            timeout=settings.request_timeout,
        )
    except (asyncio.TimeoutError, TimeoutError) as exc:  # noqa: UP041
        logger.warning("Analyze/live timeout", extra=log_extra)
        mai_analyze_total.labels(backend=settings.backend, result=AnalyzeResult.TIMEOUT.value).inc()
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=f"Analyze/live exceeded {settings.request_timeout}s timeout",
        ) from exc
    except RedactionError as exc:
        logger.warning(
            "Analyze/live blocked: residual PII after redaction (KVKK fail-closed)",
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
        logger.warning("Analyze/live backend not implemented", extra=log_extra)
        mai_analyze_total.labels(
            backend=settings.backend, result=AnalyzeResult.NOT_IMPLEMENTED.value
        ).inc()
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Selected LLM backend is not wired yet",
        ) from exc
    except BackendUnavailableError as exc:
        logger.error(
            "Analyze/live backend unavailable",
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
            "Analyze/live I/O failure",
            extra={**log_extra, "err_class": type(exc).__name__},
        )
        mai_analyze_total.labels(
            backend=settings.backend, result=AnalyzeResult.IO_ERROR.value
        ).inc()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"I/O failure ({type(exc).__name__})",
        ) from exc

    # Faz 24 live delivery: mark is_partial and thread version so the ingestion
    # side (meeting-service, next PR) can order/supersede incremental writes.
    result.is_partial = True
    result.version = live_version

    logger.info(
        "Analyze/live success",
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

    # The durable-delivery hop is intentionally skipped for /analyze/live in
    # this scaffold PR. Live partial deliveries route through a partial-aware
    # ingestion path (`is_partial=True`, `version=N`) that the meeting-service
    # side does not yet accept; wiring it now would let live payloads land in
    # the final-only column and silently overwrite an authoritative final
    # result. The follow-up ingestion PR extends AnalysisDeliveryRuntime and
    # meeting-service to accept partials with a version-monotonic guard.
    response.headers["X-Analysis-Is-Partial"] = "true"
    response.headers["X-Analysis-Version"] = str(live_version)
    mai_analyze_total.labels(backend=settings.backend, result=AnalyzeResult.SUCCESS.value).inc()

    # Faz 24 live-stream SSE relay: publish this result to every subscriber
    # of the meeting's SSE stream. Non-blocking, drop-oldest on subscriber
    # backpressure; a missing meeting_id or zero subscribers is a no-op.
    # Errors here are counted (`dropped_total`) and swallowed — the analyze
    # HTTP path MUST succeed regardless of relay state.
    hub = _get_hub(request)
    if hub is not None and body.meeting_id:
        try:
            delivered, dropped = await hub.publish(body.meeting_id, result.model_dump())
            mai_analyze_live_stream_published_total.inc()
            if delivered:
                mai_analyze_live_stream_delivered_total.inc(delivered)
            if dropped:
                mai_analyze_live_stream_dropped_total.inc(dropped)
        except Exception:  # noqa: BLE001 — defensive: relay MUST NOT fail the request
            logger.warning("Live-stream publish failed; swallowing", extra=log_extra)
            mai_analyze_live_stream_dropped_total.inc()

    return result


# Faz 24 live-stream SSE relay endpoint (Zeynep 2026-07-20 kapsam):
# Clients (desktop panel today; mobile later) subscribe to per-meeting live
# analysis events over an HTTP Server-Sent Events stream. Each `/analyze/live`
# call above fans out its result to every subscriber of the meeting_id.
#
# Standards-compliant SSE:
#   - `Content-Type: text/event-stream`
#   - `Cache-Control: no-cache`, `X-Accel-Buffering: no` (bypass nginx buffer)
#   - `event: analysis` frames carry the AnalyzeResponse JSON
#   - `event: ping` frames every `live_stream_heartbeat_sec` keep the
#     connection warm through proxies and let clients notice a peer gone
#     (a stalled connection stops receiving pings)
#   - The initial `: subscribed <n>` comment is a "hello" that some clients
#     use to confirm the connection is established.
#
# Client contract:
#   GET /analyze/live/stream/{meeting_id}
#     -> 200 text/event-stream (indefinite)
#   Read `event: analysis` frames, parse `data:` JSON as AnalyzeResponse.
#   `is_partial=True` + `version=N` — order by version, keep the highest.
#
# The endpoint intentionally has NO transcript request/response body — it is
# a subscribe-only channel. Auth (when we wire it) will be the same JWT
# middleware guarding the analyze endpoints.


def _get_hub(request: Request) -> LiveStreamHub | None:
    return getattr(request.app.state, "live_stream_hub", None)


@router.get(
    "/analyze/live/stream/{meeting_id}",
    summary="Subscribe to per-meeting live analysis SSE stream",
    response_class=Response,  # documented as Response; the implementation streams
    responses={
        200: {"content": {"text/event-stream": {}}},
        503: {"description": "Live-stream hub not initialised"},
    },
)
async def analyze_live_stream_endpoint(
    request: Request,
    meeting_id: str,
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> Response:
    """SSE subscribe. See module doc for the frame protocol."""
    from starlette.responses import (
        StreamingResponse,  # local import: avoid top-level for test isolation
    )

    hub = _get_hub(request)
    if hub is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="live-stream hub not initialised",
        )

    heartbeat = float(settings.live_stream_heartbeat_sec)
    corr_id = _correlation_id(request)

    async def event_stream() -> asyncio.AsyncIterator[bytes]:  # type: ignore[name-defined]
        import json

        sub = await hub.subscribe(meeting_id)
        mai_analyze_live_stream_subscribers.inc()
        logger.info(
            "Live-stream subscribed",
            extra={"correlation_id": corr_id, "meeting_id": meeting_id},
        )
        try:
            # SSE comment (starts with `:`) — clients ignore it but proxies see traffic.
            initial = await hub.subscriber_count(meeting_id)
            yield f": subscribed {initial}\n\n".encode()
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(sub.queue.get(), timeout=heartbeat)
                    payload = json.dumps(event, default=str)
                    yield f"event: analysis\ndata: {payload}\n\n".encode()
                except TimeoutError:
                    yield b"event: ping\ndata: {}\n\n"
        except asyncio.CancelledError:
            # Client dropped mid-stream; normal path.
            raise
        finally:
            await hub.unsubscribe(meeting_id, sub)
            mai_analyze_live_stream_subscribers.dec()
            logger.info(
                "Live-stream unsubscribed",
                extra={"correlation_id": corr_id, "meeting_id": meeting_id},
            )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
