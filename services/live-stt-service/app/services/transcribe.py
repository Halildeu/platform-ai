"""Whisper transcribe wrapper.

faster-whisper'ı thin layer ile sarar. Model lazy-load (ilk request'te load,
sonra cache). Thread-safe inference için global lock (CPU PoC; GPU sürümünde
multi-stream re-evaluate edilecek).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import BinaryIO

from app.core.config import Settings
from app.models.schemas import TranscribeResponse, TranscriptSegment

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WhisperInferenceResult:
    """Internal result type — separated from API schema for layer isolation."""

    text: str
    language: str
    language_probability: float
    duration: float
    elapsed_ms: int
    segments: list[TranscriptSegment]


class TranscribeService:
    """Whisper-backed transcribe service.

    Lazy-loads the model on first inference. The lock prevents concurrent
    CPU inference contention; for GPU rollout we will re-evaluate stream-
    parallelism.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._model: object | None = None
        self._lock = threading.Lock()

    def _ensure_model(self) -> object:
        """Lazy model load — first call pays the latency."""
        if self._model is None:
            with self._lock:
                if self._model is None:
                    # Local import to keep import cost out of test/CI default path
                    from faster_whisper import WhisperModel

                    logger.info(
                        "Loading Whisper model",
                        extra={
                            "model": self._settings.model_name,
                            "device": self._settings.device,
                            "compute_type": self._settings.compute_type,
                        },
                    )
                    self._model = WhisperModel(
                        self._settings.model_name,
                        device=self._settings.device,
                        compute_type=self._settings.compute_type,
                    )
        assert self._model is not None
        return self._model

    def transcribe(self, audio: BinaryIO | str) -> TranscribeResponse:
        """Run inference and return API response.

        Args:
            audio: file-like object or path. faster-whisper accepts both.

        Returns:
            TranscribeResponse with text + segments + meta.
        """
        model = self._ensure_model()
        language = None if self._settings.language == "auto" else self._settings.language

        start = time.perf_counter()
        with self._lock:
            # faster-whisper API: transcribe() returns (segments_iterator, info)
            segments_iter, info = model.transcribe(  # type: ignore[attr-defined]
                audio,
                language=language,
                beam_size=self._settings.beam_size,
                vad_filter=self._settings.vad_filter,
            )
            segments = [
                TranscriptSegment(
                    id=idx,
                    start=seg.start,
                    end=seg.end,
                    text=seg.text.strip(),
                    avg_logprob=getattr(seg, "avg_logprob", None),
                    no_speech_prob=getattr(seg, "no_speech_prob", None),
                )
                for idx, seg in enumerate(segments_iter)
            ]
        elapsed_ms = int((time.perf_counter() - start) * 1000)

        full_text = " ".join(s.text for s in segments).strip()

        return TranscribeResponse(
            text=full_text,
            language=info.language,
            language_probability=info.language_probability,
            duration=info.duration,
            elapsed_ms=elapsed_ms,
            model=self._settings.model_name,
            compute_type=self._settings.compute_type,
            device=self._settings.device,
            segments=segments,
        )

    @property
    def model_loaded(self) -> bool:
        return self._model is not None


_service: TranscribeService | None = None


def get_service(settings: Settings) -> TranscribeService:
    """Singleton accessor."""
    global _service
    if _service is None:
        _service = TranscribeService(settings)
    return _service
