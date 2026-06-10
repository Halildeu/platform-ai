from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.core.config import Settings
from app.models.schemas import FinalSttJob
from app.services.transcribe import FinalTranscriber


@dataclass
class FakeSegment:
    start: float
    end: float
    text: str
    avg_logprob: float = -0.2
    no_speech_prob: float = 0.01


@dataclass
class FakeInfo:
    duration: float = 12.0
    language: str = "tr"
    language_probability: float = 0.99


class FakeModel:
    def transcribe(self, _audio: str, **_kwargs: object) -> tuple[list[FakeSegment], FakeInfo]:
        return [FakeSegment(0, 12, "kararı kaydettik.")], FakeInfo()


def make_job(path: Path, duration: float = 12.0) -> FinalSttJob:
    return FinalSttJob(
        sessionId="session",
        chunkSeq=3,
        audioPath=str(path),
        audioDurationSec=duration,
        committedText="Toplantıda kararı",
        draftText="geçici metin",
        correlationId="corr",
    )


def test_transcribe_validates_and_merges(tmp_path: Path) -> None:
    audio = tmp_path / "chunk.wav"
    audio.write_bytes(b"fake")
    settings = Settings(audio_root=tmp_path, device="cpu", compute_type="int8")
    result = FinalTranscriber(settings, model=FakeModel()).transcribe(make_job(audio))
    assert result.revised_text == "Toplantıda kararı kaydettik."
    assert result.model == "large-v3"
    assert result.audio_duration_sec == 12.0


def test_transcribe_rejects_short_chunk(tmp_path: Path) -> None:
    audio = tmp_path / "chunk.wav"
    audio.write_bytes(b"fake")
    settings = Settings(audio_root=tmp_path)
    with pytest.raises(ValueError, match="between 10 and 15"):
        FinalTranscriber(settings, model=FakeModel()).transcribe(make_job(audio, duration=5))


def test_transcribe_rejects_path_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.wav"
    outside.write_bytes(b"fake")
    settings = Settings(audio_root=root)
    with pytest.raises(ValueError, match="outside"):
        FinalTranscriber(settings, model=FakeModel()).transcribe(make_job(outside))


def test_pinned_model_hash_is_verified(tmp_path: Path) -> None:
    model_path = tmp_path / "model"
    model_path.mkdir()
    model_bytes = b"pinned-model"
    (model_path / "model.bin").write_bytes(model_bytes)
    digest = hashlib.sha256(model_bytes).hexdigest()
    settings = Settings(model_path=model_path, model_sha256=digest)
    assert FinalTranscriber(settings)._verify_model_artifact(model_path) == model_path.resolve()


def test_pinned_model_hash_mismatch_is_rejected(tmp_path: Path) -> None:
    model_path = tmp_path / "model"
    model_path.mkdir()
    (model_path / "model.bin").write_bytes(b"wrong")
    settings = Settings(model_path=model_path, model_sha256="0" * 64)
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        FinalTranscriber(settings)._verify_model_artifact(model_path)
