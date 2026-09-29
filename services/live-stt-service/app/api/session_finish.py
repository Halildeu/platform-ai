"""POST /session/{key}/finish — gateway finish handshake (#3746 AI-D3).

Contract: docs/contracts/live-stt-session-finish.v1.schema.json (design D4).
The gateway calls this once when it closes a session; the handler hands the
session's audio out of the store and schedules the attribution batch on a
single-flight background thread, then answers 202 immediately — the live
path never waits on diarization. Unknown or already-consumed keys are a
no-op 202 as well (duplicate finish must not error the gateway's close path);
the job itself reports the fail-closed cancel in metadata-only logs.

The router is mounted only when session_attribution_enabled is true, so the
surface does not exist otherwise.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Path, Response
from pydantic import BaseModel, Field

from app.core.config import Settings
from app.services.session_attribution_job import (
    AttributionPublisher,
    FinishEnvelope,
    JobResult,
    run_attribution_job,
)
from app.services.session_audio_store import SessionAudioStore
from app.services.session_diarization_runner import RunnerConfig

__all__ = ["build_finish_router"]

logger = logging.getLogger("live-stt.session-finish")


class SessionFinishRequest(BaseModel):
    """liveSttSessionFinish.v1 — identity envelope + integrity total."""

    schema_id: Annotated[str, Field(alias="schema", pattern=r"^liveSttSessionFinish\.v1$")]
    tenant_id: Annotated[str, Field(alias="tenantId", min_length=1, max_length=64)]
    meeting_id: Annotated[
        str,
        Field(
            alias="meetingId",
            pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        ),
    ]
    source_session_id: Annotated[str, Field(alias="sourceSessionId", min_length=1, max_length=128)]
    transport_epoch: Annotated[int, Field(alias="transportEpoch", ge=0)]
    expected_sample_count: Annotated[
        int, Field(alias="expectedSampleCount", ge=16_000, le=2_000_000_000)
    ]

    model_config = {"populate_by_name": True, "extra": "forbid"}


def build_finish_router(
    *,
    settings: Settings,
    store: SessionAudioStore,
    publisher: AttributionPublisher,
    job: Callable[..., JobResult] = run_attribution_job,
) -> APIRouter:
    """Router factory: every dependency is injected so tests need no app state.

    ``single_flight`` serializes batches (one GPU); the 202 is returned before
    the job runs, so a busy batch only delays the next one, never the gateway.
    """
    router = APIRouter()
    single_flight = threading.Semaphore(1)

    runner_config = RunnerConfig(
        model_name=settings.diar_model_name,
        model_revision=settings.diar_model_revision,
        hf_token=settings.diar_hf_token,
        max_speakers=settings.diar_max_speakers,
        required_free_vram_mb=settings.diar_required_free_vram_mb,
        hard_timeout_sec=settings.diar_hard_timeout_sec,
    )

    def _run(key: str, envelope: FinishEnvelope) -> None:
        with single_flight:
            audio = store.finish(key)
            result = job(
                audio,
                envelope,
                runner_config,
                publisher=publisher,
                dominance_threshold=settings.diar_dominance_threshold,
                min_speech_ms=settings.diar_min_speech_ms,
                vram_retry_attempts=settings.diar_vram_retry_attempts,
                vram_retry_backoff_sec=settings.diar_vram_retry_backoff_sec,
            )
        logger.info(
            "session attribution job finished",
            extra={
                "correlation_id": envelope.source_session_id,
                "status": result.status.value,
                "windows": result.window_count,
                "uu_windows": result.uu_window_count,
                "elapsed_ms": result.elapsed_ms,
                "detail": result.detail,
            },
        )

    @router.post("/session/{key}/finish", status_code=202)
    def finish_session(
        key: Annotated[str, Path(min_length=1, max_length=128)],
        body: SessionFinishRequest,
    ) -> Response:
        envelope = FinishEnvelope(
            tenant_id=body.tenant_id,
            meeting_id=body.meeting_id,
            source_session_id=body.source_session_id,
            transport_epoch=body.transport_epoch,
            expected_sample_count=body.expected_sample_count,
        )
        threading.Thread(
            target=_run, args=(key, envelope), name="session-attribution-job", daemon=True
        ).start()
        return Response(status_code=202)

    return router
