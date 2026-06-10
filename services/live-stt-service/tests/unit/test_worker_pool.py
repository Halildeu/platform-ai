"""Worker pool tests for PR-stt-03 subprocess isolation."""

from __future__ import annotations

from io import BytesIO

import pytest

from app.core.config import Settings
from app.models.schemas import TranscribeResponse
from app.services.transcribe import TranscribeService
from app.services.worker import (
    InlineWorkerPool,
    ProcessWorkerPool,
    WorkerCrashedError,
    WorkerTimeoutError,
    build_worker_pool,
)


def test_process_backend_is_default() -> None:
    settings = Settings(model_name="tiny")

    pool = build_worker_pool(settings)

    assert isinstance(pool, ProcessWorkerPool)
    pool.close()


def test_inline_backend_keeps_unit_tests_in_process() -> None:
    settings = Settings(model_name="tiny", worker_backend="inline")

    pool = build_worker_pool(settings)

    assert isinstance(pool, InlineWorkerPool)


def test_transcribe_service_delegates_to_worker_pool() -> None:
    class FakePool:
        model_loaded = False

        def transcribe(self, audio, timeout_sec=None):  # type: ignore[no-untyped-def]
            assert isinstance(audio, BytesIO)
            assert timeout_sec is None
            self.model_loaded = True
            return TranscribeResponse(
                text="ok",
                language="tr",
                language_probability=1.0,
                duration=1.0,
                elapsed_ms=3,
                model="tiny",
                compute_type="int8",
                device="cpu",
                segments=[],
            )

    settings = Settings(model_name="tiny", worker_backend="inline")
    pool = FakePool()
    service = TranscribeService(settings, worker_pool=pool)

    result = service.transcribe(BytesIO(b"audio"))

    assert result.text == "ok"
    assert service.model_loaded is True


def test_worker_crash_error_is_available_for_api_mapping() -> None:
    with pytest.raises(WorkerCrashedError):
        raise WorkerCrashedError("worker died")


def test_process_worker_timeout_kills_and_respawns_slot() -> None:
    """Timeout path terminates/kills the busy worker and raises WorkerTimeoutError."""
    import queue
    import threading

    class FakeTaskQueue:
        def __init__(self) -> None:
            self.items = []

        def put(self, item):  # type: ignore[no-untyped-def]
            self.items.append(item)

    class FakeResultQueue:
        def get(self, timeout):  # type: ignore[no-untyped-def]
            raise queue.Empty

    class FakeSlot:
        index = 0
        model_loaded = False

        def __init__(self) -> None:
            self.task_queue = FakeTaskQueue()
            self.result_queue = FakeResultQueue()
            self.kill_calls = 0

        def is_alive(self) -> bool:
            return True

        def kill_for_timeout(self, grace_sec: float) -> None:
            assert grace_sec == 0.01
            self.kill_calls += 1

    slot = FakeSlot()
    pool = ProcessWorkerPool.__new__(ProcessWorkerPool)
    pool._slots = [slot]  # type: ignore[attr-defined]
    pool._available = threading.BoundedSemaphore(value=1)  # type: ignore[attr-defined]
    pool._lock = threading.Lock()  # type: ignore[attr-defined]
    pool._next_slot = 0  # type: ignore[attr-defined]
    pool._busy = set()  # type: ignore[attr-defined]
    pool._kill_grace_sec = 0.01  # type: ignore[attr-defined]

    with pytest.raises(WorkerTimeoutError):
        pool.transcribe(BytesIO(b"audio"), timeout_sec=0.01)

    assert slot.kill_calls == 1
    assert slot.task_queue.items[0]["type"] == "transcribe"
