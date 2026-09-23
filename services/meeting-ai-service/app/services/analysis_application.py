"""Shared application use case for API and transcript-ready worker analysis."""

from __future__ import annotations

import asyncio
import functools
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from app.api.metrics import mai_analysis_deadline_total, mai_analysis_stage_seconds
from app.core.config import Settings
from app.models.schemas import AnalyzeResponse, LiveAnalysisCursor
from app.services.analyze import MeetingAnalysisService


class AnalysisTranscriptTooLargeError(ValueError):
    """The canonical/request transcript exceeds the configured memory guard."""


class AnalysisTimeoutError(TimeoutError):
    """The analysis did not finish inside the application deadline."""

    def __init__(self, stage: str) -> None:
        super().__init__("analysis deadline exceeded")
        self.stage = stage


@dataclass(frozen=True)
class AnalysisCommand:
    transcript: str = field(repr=False)
    meeting_id: str | None = None
    session_id: str | None = None
    finalization_version: int | None = None
    analysis_run_id: str | None = None
    generated_at: datetime | None = None
    segments: list[dict[str, object]] | None = field(default=None, repr=False)
    live: bool = False
    live_cursor: LiveAnalysisCursor | None = field(default=None, repr=False)


@dataclass(frozen=True)
class AnalysisExecution:
    result: AnalyzeResponse
    analysis_run_id: str | None


class AnalysisResultPersister(Protocol):
    async def __call__(
        self,
        command: AnalysisCommand,
        result: AnalyzeResponse,
    ) -> str | None: ...


class AnalysisApplicationService:
    """Run redaction/analysis and durably persist through an injected boundary."""

    def __init__(self, settings: Settings, analyzer: MeetingAnalysisService) -> None:
        self._settings = settings
        self._analyzer = analyzer
        self._analysis_slots = asyncio.Semaphore(settings.analysis_max_concurrency)

    async def execute(
        self,
        command: AnalysisCommand,
        *,
        persist: AnalysisResultPersister,
    ) -> AnalysisExecution:
        if len(command.transcript) > self._settings.max_transcript_chars:
            raise AnalysisTranscriptTooLargeError("transcript exceeds configured limit")
        started_at = time.monotonic()
        queue_outcome = "cancelled"
        try:
            await asyncio.wait_for(
                self._analysis_slots.acquire(),
                timeout=self._settings.request_timeout,
            )
            queue_outcome = "acquired"
        except (asyncio.TimeoutError, TimeoutError) as exc:  # noqa: UP041
            queue_outcome = "timeout"
            mai_analysis_deadline_total.labels(stage="queue").inc()
            raise AnalysisTimeoutError("queue") from exc
        finally:
            mai_analysis_stage_seconds.labels(stage="queue", outcome=queue_outcome).observe(
                time.monotonic() - started_at
            )
        remaining = self._settings.request_timeout - (time.monotonic() - started_at)
        if remaining <= 0:
            self._analysis_slots.release()
            mai_analysis_deadline_total.labels(stage="queue").inc()
            raise AnalysisTimeoutError("queue")
        loop = asyncio.get_running_loop()
        try:
            future = loop.run_in_executor(
                None,
                functools.partial(
                    self._run_analyzer,
                    command.transcript,
                    command.segments,
                    command.live,
                    command.live_cursor,
                ),
            )
        except BaseException:
            self._analysis_slots.release()
            raise
        future.add_done_callback(lambda _future: self._analysis_slots.release())
        remaining = self._settings.request_timeout - (time.monotonic() - started_at)
        if remaining <= 0:
            mai_analysis_deadline_total.labels(stage="worker").inc()
            raise AnalysisTimeoutError("worker")
        try:
            result = await asyncio.wait_for(asyncio.shield(future), timeout=remaining)
        except (asyncio.TimeoutError, TimeoutError) as exc:  # noqa: UP041
            mai_analysis_deadline_total.labels(stage="worker").inc()
            raise AnalysisTimeoutError("worker") from exc
        run_id = await persist(command, result)
        return AnalysisExecution(result=result, analysis_run_id=run_id)

    def _run_analyzer(
        self,
        transcript: str,
        segments: list[dict[str, object]] | None,
        live: bool = False,
        cursor: LiveAnalysisCursor | None = None,
    ) -> AnalyzeResponse:
        started_at = time.monotonic()
        outcome = "error"
        try:
            result = (
                self._analyzer.analyze(transcript, segments, live=True, live_cursor=cursor)
                if live
                else self._analyzer.analyze(transcript, segments)
            )
            outcome = "success"
            return result
        finally:
            # A timed-out caller does not stop a synchronous worker. Measure its
            # actual lifetime so capacity pressure remains visible after HTTP 504.
            mai_analysis_stage_seconds.labels(stage="worker", outcome=outcome).observe(
                time.monotonic() - started_at
            )

    @property
    def model_loaded(self) -> bool:
        return self._analyzer.model_loaded
