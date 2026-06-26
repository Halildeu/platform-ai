"""POST /ask — #162 PR-llm-03 post-meeting ask-AI.

Answer a question from the (redacted) transcript with a citation. KVKK: raw
transcript is never logged (only lengths/metadata).
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from starlette.concurrency import run_in_threadpool

from app.core.config import Settings, get_settings
from app.models.schemas import AskRequest, AskResponse
from app.services.ask import answer_question
from app.services.redact import RedactionError

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post(
    "/ask",
    response_model=AskResponse,
    status_code=status.HTTP_200_OK,
    summary="Ask-AI over a meeting transcript (grounded)",
)
async def ask_endpoint(
    request: Request,
    body: AskRequest,
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> AskResponse:
    if len(body.transcript) > settings.max_transcript_chars:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Transcript too large (limit {settings.max_transcript_chars} chars)",
        )
    corr_id = getattr(request.state, "correlation_id", "")
    try:
        result = await run_in_threadpool(answer_question, body.transcript, body.question, settings)
    except RedactionError as exc:
        logger.warning(
            "Ask blocked: residual PII after redaction (KVKK fail-closed)",
            extra={"correlation_id": corr_id, "err_class": type(exc).__name__},
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Redaction could not guarantee PII removal; blocked ({exc})",
        ) from exc
    except NotImplementedError as exc:
        logger.warning(
            "Ask backend not implemented",
            extra={"correlation_id": corr_id, "err_class": type(exc).__name__},
        )
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Selected LLM backend is not wired yet",
        ) from exc
    except httpx.HTTPError as exc:
        # transcript-free log
        logger.warning(
            "Ask backend error",
            extra={"correlation_id": corr_id, "err_class": type(exc).__name__},
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"LLM backend unreachable ({type(exc).__name__})",
        ) from exc

    logger.info(
        "Ask answered",
        extra={
            "correlation_id": corr_id,
            "meeting_id": body.meeting_id or "",
            "grounded": result.grounded,
            "elapsed_ms": result.elapsed_ms,
        },
    )
    return result
