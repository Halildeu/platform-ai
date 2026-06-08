"""Mock diarizer unit tests (deterministic, GPU-free)."""

from __future__ import annotations

from app.core.config import Settings
from app.services.diarize import MockDiarizer, PyannoteDiarizer, build_diarizer
from tests.conftest import make_wav


def _settings(**kwargs: object) -> Settings:
    return Settings(**kwargs)  # type: ignore[arg-type]


def test_segments_cover_duration_and_alternate_speakers() -> None:
    diar = MockDiarizer(_settings(mock_num_speakers=2, mock_turn_sec=2.5))
    result = diar.diarize(make_wav(6.0))

    assert result.backend == "mock"
    assert result.duration == 6.0
    # turns: [0,2.5], [2.5,5], [5,6]
    assert [round(s.start, 1) for s in result.segments] == [0.0, 2.5, 5.0]
    assert result.segments[-1].end == 6.0
    assert result.num_speakers == 2
    assert result.segments[0].speaker == "SPEAKER_00"
    assert result.segments[1].speaker == "SPEAKER_01"
    assert result.segments[2].speaker == "SPEAKER_00"


def test_single_speaker_config() -> None:
    diar = MockDiarizer(_settings(mock_num_speakers=1, mock_turn_sec=2.0))
    result = diar.diarize(make_wav(5.0))
    assert result.num_speakers == 1
    assert {s.speaker for s in result.segments} == {"SPEAKER_00"}


def test_non_wav_falls_back_to_default_duration() -> None:
    diar = MockDiarizer(_settings(mock_default_duration_sec=4.0, mock_turn_sec=2.0))
    result = diar.diarize(b"not-a-wav-file")
    assert result.duration == 4.0
    assert len(result.segments) == 2


def test_build_diarizer_selects_backend() -> None:
    assert isinstance(build_diarizer(_settings(backend="mock")), MockDiarizer)
    assert isinstance(build_diarizer(_settings(backend="pyannote")), PyannoteDiarizer)


def test_pyannote_stub_raises() -> None:
    diar = PyannoteDiarizer(_settings(backend="pyannote"))
    assert diar.model_loaded is False
    try:
        diar.diarize(make_wav(2.0))
    except NotImplementedError:
        return
    raise AssertionError("expected NotImplementedError")
