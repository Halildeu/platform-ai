"""PyannoteDiarizer wiring tests (#48) — fake pipeline, no pyannote install.

The real pipeline is injected via `pipeline_factory`, so these tests verify the
adapter logic (temp-file handling, annotation -> SpeakerSegment conversion,
anonymous labels, lazy load) without torch/pyannote present.
"""

from __future__ import annotations

import io
import wave

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.services.diarize import PyannoteDiarizer, build_diarizer


class _FakeSegment:
    def __init__(self, start: float, end: float) -> None:
        self.start = start
        self.end = end


class _FakeAnnotation:
    def __init__(self, tracks: list[tuple[float, float, str]]) -> None:
        self._tracks = tracks

    def itertracks(self, yield_label: bool = False):
        for start, end, label in self._tracks:
            yield _FakeSegment(start, end), "track", label


class _FakePipeline:
    def __init__(self, annotation: _FakeAnnotation) -> None:
        self._annotation = annotation
        self.called_with: str | None = None

    def __call__(self, audio_path: str) -> _FakeAnnotation:
        self.called_with = audio_path
        return self._annotation


def _wav_bytes(duration_sec: float = 2.0, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(duration_sec * rate))
    return buf.getvalue()


def _settings() -> Settings:
    return Settings(backend="pyannote", hf_token="hf_test_token", device="cpu")


def test_pyannote_backend_requires_hf_token() -> None:
    with pytest.raises(ValidationError):
        Settings(backend="pyannote", hf_token="")


def test_build_diarizer_selects_pyannote() -> None:
    diarizer = build_diarizer(_settings())
    assert isinstance(diarizer, PyannoteDiarizer)


def test_pipeline_lazy_loaded_not_at_construction() -> None:
    calls: list[Settings] = []

    def factory(s: Settings) -> _FakePipeline:
        calls.append(s)
        return _FakePipeline(_FakeAnnotation([]))

    d = PyannoteDiarizer(_settings(), pipeline_factory=factory)
    assert calls == []
    assert d.model_loaded is False
    d.diarize(_wav_bytes())
    assert len(calls) == 1
    assert d.model_loaded is True
    d.diarize(_wav_bytes())
    assert len(calls) == 1  # loaded once, reused


def test_annotation_converted_to_anonymous_segments() -> None:
    annotation = _FakeAnnotation(
        [(0.0, 1.25, "SPEAKER_00"), (1.25, 2.0, "SPEAKER_01"), (2.0, 2.5, "SPEAKER_00")]
    )
    pipeline = _FakePipeline(annotation)
    d = PyannoteDiarizer(_settings(), pipeline_factory=lambda _s: pipeline)

    resp = d.diarize(_wav_bytes(duration_sec=2.5))

    assert resp.backend == "pyannote"
    assert resp.num_speakers == 2
    assert [s.speaker for s in resp.segments] == ["SPEAKER_00", "SPEAKER_01", "SPEAKER_00"]
    assert resp.segments[0].start == 0.0
    assert resp.segments[0].end == 1.25
    assert resp.duration == pytest.approx(2.5, abs=0.01)
    # KVKK: anonymous labels only — no embedding/voiceprint fields exist.
    assert not hasattr(resp.segments[0], "embedding")
    # Pipeline received a temp file path, not raw bytes.
    assert pipeline.called_with is not None
    assert pipeline.called_with.endswith(".wav")
