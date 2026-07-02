"""Worker pool tests for PR-stt-03 subprocess isolation."""

from __future__ import annotations

from io import BytesIO
from types import MethodType

import pytest

from app.core.config import Settings
from app.models.schemas import TranscribeResponse
from app.services.transcribe import TranscribeService
from app.services.worker import (
    InlineWorkerPool,
    ProcessWorkerPool,
    WorkerCrashedError,
    WorkerTimeoutError,
    _WorkerSlot,
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

        def transcribe(self, audio, timeout_sec=None, language=None):  # type: ignore[no-untyped-def]
            assert isinstance(audio, BytesIO)
            assert timeout_sec is None
            assert language is None
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


def test_worker_slot_timeout_terminate_kill_respawn_sequence() -> None:
    """Hard timeout uses terminate, grace wait, kill fallback, queue reset, respawn."""

    class FakeProcess:
        def __init__(self) -> None:
            self.alive = True
            self.calls: list[str] = []

        def is_alive(self) -> bool:
            return self.alive

        def terminate(self) -> None:
            self.calls.append("terminate")

        def join(self, timeout: float) -> None:
            self.calls.append(f"join:{timeout}")

        def kill(self) -> None:
            self.calls.append("kill")
            self.alive = False

    class FakeCtx:
        def __init__(self) -> None:
            self.queue_count = 0

        def Queue(self, maxsize: int):  # type: ignore[no-untyped-def]  # noqa: N802
            self.queue_count += 1
            return {"maxsize": maxsize, "queue_no": self.queue_count}

    process = FakeProcess()
    ctx = FakeCtx()
    slot = _WorkerSlot.__new__(_WorkerSlot)
    slot.ctx = ctx  # type: ignore[attr-defined]
    slot.process = process  # type: ignore[attr-defined]
    slot.task_queue = "old-task-queue"  # type: ignore[attr-defined]
    slot.result_queue = "old-result-queue"  # type: ignore[attr-defined]
    slot.model_loaded = True  # type: ignore[attr-defined]

    start_calls: list[str] = []

    def fake_start(self) -> None:  # type: ignore[no-untyped-def]
        start_calls.append("start")
        self.process = "respawned-worker"

    slot.start = MethodType(fake_start, slot)  # type: ignore[method-assign]

    _WorkerSlot.kill_for_timeout(slot, grace_sec=2.0)

    assert process.calls == ["terminate", "join:2.0", "kill", "join:2.0"]
    assert start_calls == ["start"]
    assert slot.task_queue == {"maxsize": 1, "queue_no": 1}
    assert slot.result_queue == {"maxsize": 1, "queue_no": 2}
    assert slot.model_loaded is False
    assert slot.process == "respawned-worker"
