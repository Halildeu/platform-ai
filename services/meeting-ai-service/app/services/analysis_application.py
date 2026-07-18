"""Shared application use case for API and transcript-ready worker analysis."""

from __future__ import annotations

import asyncio
import functools
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from app.core.config import Settings
from app.models.schemas import AnalyzeResponse
from app.services.analyze import MeetingAnalysisService


class AnalysisTranscriptTooLargeError(ValueError):
    """The canonical/request transcript exceeds the configured memory guard."""


class AnalysisTimeoutError(TimeoutError):
    """The analysis did not finish inside the application deadline."""


@dataclass(frozen=True)
class AnalysisCommand:
    transcript: str = field(repr=False)
    meeting_id: str | None = None
    session_id: str | None = None
    finalization_version: int | None = None
    analysis_run_id: str | None = None
    generated_at: datetime | None = None
    segments: list[dict[str, object]] | None = field(default=None, repr=False)


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
        try:
            await asyncio.wait_for(
                self._analysis_slots.acquire(),
                timeout=self._settings.request_timeout,
            )
        except (asyncio.TimeoutError, TimeoutError) as exc:  # noqa: UP041
            raise AnalysisTimeoutError("analysis deadline exceeded") from exc
        loop = asyncio.get_running_loop()
        try:
            future = loop.run_in_executor(
                None,
                functools.partial(
                    self._analyzer.analyze,
                    command.transcript,
                    command.segments,
                ),
            )
        except BaseException:
            self._analysis_slots.release()
            raise
        future.add_done_callback(lambda _future: self._analysis_slots.release())
        remaining = self._settings.request_timeout - (time.monotonic() - started_at)
        if remaining <= 0:
            raise AnalysisTimeoutError("analysis deadline exceeded")
        try:
            result = await asyncio.wait_for(asyncio.shield(future), timeout=remaining)
        except (asyncio.TimeoutError, TimeoutError) as exc:  # noqa: UP041
            raise AnalysisTimeoutError("analysis deadline exceeded") from exc
        run_id = await persist(command, result)
        return AnalysisExecution(result=result, analysis_run_id=run_id)

    @property
    def model_loaded(self) -> bool:
        return self._analyzer.model_loaded
