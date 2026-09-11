"""Deadline behavior for synchronous analyzers executed by the async use case."""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
from prometheus_client import REGISTRY

import app.services.analysis_application as application_module
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
            with pytest.raises(AnalysisTimeoutError) as worker_error:
                await application.execute(
                    AnalysisCommand(transcript="bounded input"),
                    persist=persist,  # type: ignore[arg-type]
                )
            assert worker_error.value.stage == "worker"
            with pytest.raises(AnalysisTimeoutError) as queue_error:
                await application.execute(
                    AnalysisCommand(transcript="must not start another worker"),
                    persist=persist,  # type: ignore[arg-type]
                )
            assert queue_error.value.stage == "queue"
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


def test_expired_budget_after_capacity_acquisition_never_starts_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticks = iter((0.0, 1.0, 1.0))
    monkeypatch.setattr(application_module, "time", SimpleNamespace(monotonic=lambda: next(ticks)))

    async def scenario() -> None:
        analyzer = BlockingAnalyzer()
        application = AnalysisApplicationService(
            Settings(request_timeout=1, analysis_max_concurrency=1),
            analyzer,  # type: ignore[arg-type]
        )
        with pytest.raises(AnalysisTimeoutError) as error:
            await application.execute(
                AnalysisCommand(transcript="expired before dispatch"),
                persist=lambda _command, _result: asyncio.sleep(0, result=None),
            )
        assert error.value.stage == "queue"
        assert analyzer.calls == 0
        assert not application._analysis_slots.locked()

    asyncio.run(scenario())


def test_worker_metrics_include_failure_without_transcript_labels() -> None:
    class FailingAnalyzer:
        def analyze(self, _transcript: str, _segments: object) -> object:
            raise ValueError("synthetic failure")

    labels = {"stage": "worker", "outcome": "error"}
    metric = "mai_analysis_stage_seconds_count"
    before = REGISTRY.get_sample_value(metric, labels) or 0.0

    async def scenario() -> None:
        application = AnalysisApplicationService(
            Settings(request_timeout=1),
            FailingAnalyzer(),  # type: ignore[arg-type]
        )
        with pytest.raises(ValueError, match="synthetic failure"):
            await application.execute(
                AnalysisCommand(transcript="must not appear in metric labels"),
                persist=lambda _command, _result: asyncio.sleep(0, result=None),
            )

    asyncio.run(scenario())
    assert REGISTRY.get_sample_value(metric, labels) == before + 1
    for family in REGISTRY.collect():
        if family.name == "mai_analysis_stage_seconds":
            assert all(
                set(sample.labels) <= {"stage", "outcome", "le"} for sample in family.samples
            )


def test_cancelled_waiter_does_not_release_running_worker_capacity() -> None:
    async def scenario() -> None:
        analyzer = BlockingAnalyzer()
        application = AnalysisApplicationService(
            Settings(request_timeout=1, analysis_max_concurrency=1),
            analyzer,  # type: ignore[arg-type]
        )

        async def execute() -> None:
            await application.execute(
                AnalysisCommand(transcript="synthetic cancellation"),
                persist=lambda _command, _result: asyncio.sleep(0, result=None),
            )

        worker = asyncio.create_task(execute())
        try:
            assert await asyncio.to_thread(analyzer.started.wait, 1.0)
            waiter = asyncio.create_task(execute())
            await asyncio.sleep(0)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker
            assert application._analysis_slots.locked()
            assert analyzer.calls == 1
        finally:
            analyzer.release.set()

    asyncio.run(scenario())
