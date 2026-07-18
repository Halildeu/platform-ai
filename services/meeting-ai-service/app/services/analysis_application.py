"""Shared application use case for API and transcript-ready worker analysis."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

import anyio

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

    async def execute(
        self,
        command: AnalysisCommand,
        *,
        persist: AnalysisResultPersister,
    ) -> AnalysisExecution:
        if len(command.transcript) > self._settings.max_transcript_chars:
            raise AnalysisTranscriptTooLargeError("transcript exceeds configured limit")
        try:
            result = await asyncio.wait_for(
                anyio.to_thread.run_sync(
                    self._analyzer.analyze,
                    command.transcript,
                    command.segments,
                    abandon_on_cancel=True,
                ),
                timeout=self._settings.request_timeout,
            )
        except (asyncio.TimeoutError, TimeoutError) as exc:  # noqa: UP041
            raise AnalysisTimeoutError("analysis deadline exceeded") from exc
        run_id = await persist(command, result)
        return AnalysisExecution(result=result, analysis_run_id=run_id)

    @property
    def model_loaded(self) -> bool:
        return self._analyzer.model_loaded
