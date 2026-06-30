"""Whisper transcribe service facade.

PR-stt-03 / #20 moves blocking Whisper inference behind a supervised
subprocess worker pool. The public service API stays small: callers submit
audio and receive a `TranscribeResponse`.
"""

from __future__ import annotations

from typing import BinaryIO

from app.core.config import Settings
from app.models.schemas import TranscribeResponse
from app.services.worker import WorkerPool, build_worker_pool


class TranscribeService:
    """Whisper-backed transcribe service.

    Production default is `STT_WORKER_BACKEND=process`, where each worker is a
    `multiprocessing.Process` that lazy-loads the model once and handles one
    inference at a time. Tests use `inline` so the faster-whisper mock remains
    in-process and no model is downloaded.
    """

    def __init__(self, settings: Settings, worker_pool: WorkerPool | None = None) -> None:
        self._settings = settings
        self._worker_pool = worker_pool or build_worker_pool(settings)

    def transcribe(
        self,
        audio: BinaryIO | str,
        timeout_sec: float | None = None,
        language: str | None = None,
    ) -> TranscribeResponse:
        """Run inference and return API response."""
        return self._worker_pool.transcribe(audio, timeout_sec=timeout_sec, language=language)

    @property
    def model_loaded(self) -> bool:
        return self._worker_pool.model_loaded


_service: TranscribeService | None = None


def get_service(settings: Settings) -> TranscribeService:
    """Singleton accessor."""
    global _service
    if _service is None:
        _service = TranscribeService(settings)
    return _service
