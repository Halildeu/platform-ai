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

        def transcribe(self, audio):  # type: ignore[no-untyped-def]
            assert isinstance(audio, BytesIO)
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
