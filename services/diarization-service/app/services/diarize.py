"""Diarization service facade.

Issue #48 skeleton: the public API stays small — submit audio bytes, receive a
`DiarizeResponse`. Two backends:

- `mock` (default): deterministic speaker turns, no model/token needed, so the
  skeleton is runnable and unit-testable on CPU.
- `pyannote`: real pyannote.audio pipeline — intentionally a stub here; wiring
  it needs a Hugging Face token + model download and is a follow-up.

No voiceprint/biometric storage in this phase.
"""

from __future__ import annotations

import time
import wave
from io import BytesIO
from typing import Protocol

from app.core.config import Settings
from app.models.schemas import DiarizeResponse, SpeakerSegment


class Diarizer(Protocol):
    """Minimal interface used by DiarizationService."""

    def diarize(self, raw_audio: bytes) -> DiarizeResponse:
        """Run diarization on raw audio bytes."""

    @property
    def model_loaded(self) -> bool:
        """Whether the backend is ready to serve."""


def _wav_duration_sec(raw_audio: bytes, fallback: float) -> float:
    """Best-effort WAV duration without decoding the whole signal.

    Falls back to a configured default for non-WAV / unreadable input so the
    mock backend never needs ffmpeg or soundfile.
    """
    try:
        with wave.open(BytesIO(raw_audio), "rb") as wav:
            frames = wav.getnframes()
            rate = wav.getframerate()
            if rate > 0:
                return frames / float(rate)
    except (wave.Error, EOFError, OSError):
        pass
    return fallback


class MockDiarizer:
    """Deterministic placeholder: split audio into round-robin speaker turns."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def diarize(self, raw_audio: bytes) -> DiarizeResponse:
        s = self._settings
        start = time.perf_counter()
        duration = _wav_duration_sec(raw_audio, s.mock_default_duration_sec)
        n_speakers = max(1, min(s.mock_num_speakers, s.max_speakers))
        turn = s.mock_turn_sec

        segments: list[SpeakerSegment] = []
        used: set[str] = set()
        t = 0.0
        idx = 0
        while t < duration:
            end = min(t + turn, duration)
            label = f"SPEAKER_{idx % n_speakers:02d}"
            used.add(label)
            segments.append(SpeakerSegment(speaker=label, start=round(t, 3), end=round(end, 3)))
            t = end
            idx += 1

        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return DiarizeResponse(
            segments=segments,
            num_speakers=len(used),
            duration=round(duration, 3),
            elapsed_ms=elapsed_ms,
            model=s.model_name,
            device=s.device,
            backend="mock",
        )

    @property
    def model_loaded(self) -> bool:
        return True


class PyannoteDiarizer:
    """Real pyannote.audio backend — not wired in the #48 skeleton."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def diarize(self, raw_audio: bytes) -> DiarizeResponse:
        raise NotImplementedError(
            "pyannote backend is not wired yet; run with DIA_BACKEND=mock. "
            "Real pipeline needs a Hugging Face token + model download (follow-up)."
        )

    @property
    def model_loaded(self) -> bool:
        return False


def build_diarizer(settings: Settings) -> Diarizer:
    if settings.backend == "pyannote":
        return PyannoteDiarizer(settings)
    return MockDiarizer(settings)


class DiarizationService:
    """Speaker-diarization service."""

    def __init__(self, settings: Settings, diarizer: Diarizer | None = None) -> None:
        self._settings = settings
        self._diarizer = diarizer or build_diarizer(settings)

    def diarize(self, raw_audio: bytes) -> DiarizeResponse:
        return self._diarizer.diarize(raw_audio)

    @property
    def model_loaded(self) -> bool:
        return self._diarizer.model_loaded


_service: DiarizationService | None = None


def get_service(settings: Settings) -> DiarizationService:
    """Singleton accessor."""
    global _service
    if _service is None:
        _service = DiarizationService(settings)
    return _service
