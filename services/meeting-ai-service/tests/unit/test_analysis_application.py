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
        self.calls = 0

    def analyze(self, _transcript: str, _segments: object) -> object:
        self.calls += 1
        self.started.set()
        self.release.wait(timeout=10.0)
        return object()


def test_timed_out_analyzer_keeps_its_capacity_slot_until_thread_exits() -> None:
    async def scenario() -> tuple[float, int]:
        analyzer = BlockingAnalyzer()
        application = AnalysisApplicationService(
            Settings(request_timeout=1, analysis_max_concurrency=1),
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
            with pytest.raises(AnalysisTimeoutError):
                await application.execute(
                    AnalysisCommand(transcript="must not start another worker"),
                    persist=persist,  # type: ignore[arg-type]
                )
        finally:
            analyzer.release.set()
        assert analyzer.started.is_set()
        await application.execute(
            AnalysisCommand(transcript="capacity is reusable after the worker exits"),
            persist=lambda _command, _result: asyncio.sleep(0, result=None),
        )
        return time.monotonic() - started_at, analyzer.calls

    elapsed, calls = asyncio.run(scenario())
    assert elapsed < 2.5
    assert calls == 2
