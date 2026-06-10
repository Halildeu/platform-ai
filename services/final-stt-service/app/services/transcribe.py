"""Lazy faster-whisper final transcription service."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

from app.core.config import Settings
from app.models.schemas import FinalSttJob, FinalSttResult, TranscriptSegment
from app.services.merge import merge_transcripts


class SegmentLike(Protocol):
    start: float
    end: float
    text: str


class InfoLike(Protocol):
    duration: float
    language: str
    language_probability: float


class WhisperLike(Protocol):
    def transcribe(
        self, audio: str, **kwargs: object
    ) -> tuple[Iterable[SegmentLike], InfoLike]: ...


class FinalTranscriber:
    def __init__(self, settings: Settings, model: WhisperLike | None = None) -> None:
        self._settings = settings
        self._model = model

    @property
    def model_loaded(self) -> bool:
        return self._model is not None

    def _ensure_model(self) -> WhisperLike:
        if self._model is None:
            from faster_whisper import WhisperModel

            model_source = self._settings.model_name
            if self._settings.model_path is not None:
                model_source = str(self._verify_model_artifact(self._settings.model_path))
            self._model = WhisperModel(
                model_source,
                device=self._settings.device,
                compute_type=self._settings.compute_type,
                local_files_only=self._settings.model_path is not None,
            )
        return self._model

    def _verify_model_artifact(self, model_path: Path) -> Path:
        resolved = model_path.resolve()
        model_bin = resolved / "model.bin"
        if not model_bin.is_file():
            raise FileNotFoundError("pinned model directory does not contain model.bin")
        expected = self._settings.model_sha256.removeprefix("sha256:").lower()
        if expected:
            digest = hashlib.sha256()
            with model_bin.open("rb") as model_file:
                for block in iter(lambda: model_file.read(1024 * 1024), b""):
                    digest.update(block)
            if digest.hexdigest() != expected:
                raise ValueError("pinned model SHA-256 mismatch")
        return resolved

    def _resolve_audio_path(self, raw_path: str) -> Path:
        root = self._settings.audio_root.resolve()
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve()
        if resolved != root and root not in resolved.parents:
            raise ValueError("audioPath is outside FINAL_STT_AUDIO_ROOT")
        if not resolved.is_file():
            raise FileNotFoundError("audioPath does not exist")
        return resolved

    def _validate_declared_duration(self, duration: float) -> None:
        if not self._settings.chunk_min_sec <= duration <= self._settings.chunk_max_sec:
            raise ValueError(
                f"audioDurationSec must be between {self._settings.chunk_min_sec:g} "
                f"and {self._settings.chunk_max_sec:g} seconds"
            )

    def transcribe(self, job: FinalSttJob) -> FinalSttResult:
        self._validate_declared_duration(job.audio_duration_sec)
        audio_path = self._resolve_audio_path(job.audio_path)
        model = self._ensure_model()
        language = None if self._settings.language == "auto" else self._settings.language

        started = time.perf_counter()
        raw_segments, info = model.transcribe(
            str(audio_path),
            language=language,
            beam_size=self._settings.beam_size,
            vad_filter=self._settings.vad_filter,
        )
        segments = [
            TranscriptSegment(
                id=index,
                start=float(segment.start),
                end=float(segment.end),
                text=str(segment.text).strip(),
                avg_logprob=getattr(segment, "avg_logprob", None),
                no_speech_prob=getattr(segment, "no_speech_prob", None),
            )
            for index, segment in enumerate(raw_segments)
        ]
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        final_chunk_text = " ".join(segment.text for segment in segments).strip()
        merged = merge_transcripts(job.committed_text, final_chunk_text)
        detected_duration = float(getattr(info, "duration", job.audio_duration_sec))
        if abs(detected_duration - job.audio_duration_sec) > self._settings.duration_tolerance_sec:
            raise ValueError("declared and detected audio duration differ")

        return FinalSttResult(
            sessionId=job.session_id,
            chunkSeq=job.chunk_seq,
            correlationId=job.correlation_id,
            revisedText=merged.text,
            finalChunkText=final_chunk_text,
            draftText=job.draft_text,
            overlapWords=merged.overlap_words,
            language=str(getattr(info, "language", self._settings.language)),
            languageProbability=float(getattr(info, "language_probability", 1.0)),
            audioDurationSec=detected_duration,
            elapsedMs=elapsed_ms,
            model=self._settings.model_name,
            computeType=self._settings.compute_type,
            device=self._settings.device,
            segments=segments,
        )


_transcriber: FinalTranscriber | None = None


def get_transcriber(settings: Settings) -> FinalTranscriber:
    global _transcriber
    if _transcriber is None:
        _transcriber = FinalTranscriber(settings)
    return _transcriber
