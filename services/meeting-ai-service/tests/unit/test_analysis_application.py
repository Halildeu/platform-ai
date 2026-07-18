"""Deadline behavior for synchronous analyzers executed by the async use case."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from app.core.config import Settings
from app.services.analysis_application import (
    AnalysisApplicationService,
    AnalysisCommand,
    AnalysisTimeoutError,
)


class BlockingAnalyzer:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def analyze(self, _transcript: str, _segments: object) -> object:
        self.started.set()
        self.release.wait(timeout=10.0)
        return object()


def test_blocking_analyzer_is_abandoned_at_application_deadline() -> None:
    async def scenario() -> float:
        analyzer = BlockingAnalyzer()
        application = AnalysisApplicationService(
            Settings(request_timeout=1),
            analyzer,  # type: ignore[arg-type]
        )

        async def persist(_command: object, _result: object) -> str | None:
            raise AssertionError("timed-out analysis must not persist")

        started_at = time.monotonic()
        try:
            with pytest.raises(AnalysisTimeoutError):
                await application.execute(
                    AnalysisCommand(transcript="bounded input"),
                    persist=persist,  # type: ignore[arg-type]
                )
        finally:
            analyzer.release.set()
        assert analyzer.started.is_set()
        return time.monotonic() - started_at

    elapsed = asyncio.run(scenario())
    assert elapsed < 2.0
