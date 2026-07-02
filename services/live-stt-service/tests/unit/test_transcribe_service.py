"""TranscribeService unit tests — mocked Whisper."""

from __future__ import annotations

import sys
from io import BytesIO
from typing import Any, cast

import pytest

from app.core.config import Settings
from app.services.transcribe import TranscribeService, get_service


@pytest.fixture
def settings() -> Settings:
    return Settings(
        model_name="tiny",
        compute_type="int8",
        device="cpu",
        language="tr",
        beam_size=5,
        vad_filter=True,
        worker_backend="inline",
    )


def test_lazy_load(settings: Settings) -> None:
    svc = TranscribeService(settings)
    assert svc.model_loaded is False
    svc.transcribe(BytesIO(b"\x00\x00"))
    assert svc.model_loaded is True


def test_transcribe_response_shape(settings: Settings) -> None:
    svc = TranscribeService(settings)
    result = svc.transcribe(BytesIO(b"\x00\x00"))

    assert result.language == "tr"
    assert result.language_probability == 0.98
    assert result.duration == 2.5
    assert result.model == "tiny"
    assert result.compute_type == "int8"
    assert result.device == "cpu"
    assert result.elapsed_ms >= 0
    assert len(result.segments) == 2
    assert result.segments[0].text == "Merhaba dünya."
    assert result.segments[1].text == "Toplantı başlıyor."
    assert result.text == "Merhaba dünya. Toplantı başlıyor."


def test_sync_transcribe_uses_stream_aligned_decode_tuning(settings: Settings) -> None:
    svc = TranscribeService(settings)
    svc.transcribe(BytesIO(b"\x00\x00"))

    fake_module = cast(Any, sys.modules["faster_whisper"])
    fake_model = fake_module.WhisperModel
    kwargs = fake_model.last_kwargs
    assert kwargs["language"] == "tr"
    assert kwargs["beam_size"] == 5
    assert kwargs["vad_filter"] is True
    assert kwargs["condition_on_previous_text"] is False
    assert kwargs["no_speech_threshold"] == 0.75
    assert kwargs["log_prob_threshold"] == -1.0
    assert kwargs["compression_ratio_threshold"] == 2.4


def test_sync_transcribe_filters_known_hallucination_segments(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.conftest import _FakeInfo, _FakeSeg

    class ArtifactWhisperModel:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def transcribe(self, _audio: object, **_kwargs: object):  # type: ignore[no-untyped-def]
            return [
                _FakeSeg(0, 0.0, 0.8, "Altyazı M.K."),
                _FakeSeg(1, 0.8, 2.5, "Toplantı yarın saat onda başlayacak."),
            ], _FakeInfo()

    monkeypatch.setattr(sys.modules["faster_whisper"], "WhisperModel", ArtifactWhisperModel)

    svc = TranscribeService(settings)
    result = svc.transcribe(BytesIO(b"\x00\x00"))

    assert result.text == "Toplantı yarın saat onda başlayacak."
    assert len(result.segments) == 1
    assert result.segments[0].text == "Toplantı yarın saat onda başlayacak."


def test_sync_transcribe_suppresses_all_hallucination_text(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.conftest import _FakeInfo, _FakeSeg

    class ArtifactOnlyWhisperModel:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def transcribe(self, _audio: object, **_kwargs: object):  # type: ignore[no-untyped-def]
            return [_FakeSeg(0, 0.0, 1.0, "İzlediğiniz için teşekkür ederim.")], _FakeInfo()

    monkeypatch.setattr(sys.modules["faster_whisper"], "WhisperModel", ArtifactOnlyWhisperModel)

    svc = TranscribeService(settings)
    result = svc.transcribe(BytesIO(b"\x00\x00"))

    assert result.text == ""
    assert result.segments == []


def test_sync_transcribe_filters_no_speech_segments(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.conftest import _FakeInfo, _FakeSeg

    class MostlySilenceWhisperModel:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def transcribe(self, _audio: object, **_kwargs: object):  # type: ignore[no-untyped-def]
            return [
                _FakeSeg(
                    0,
                    0.0,
                    1.0,
                    "Kendinize iyi geceler geçin.",
                    no_speech_prob=settings.no_speech_threshold + 0.01,
                ),
                _FakeSeg(1, 1.0, 2.0, "Toplantı kaydı başladı."),
            ], _FakeInfo()

    monkeypatch.setattr(sys.modules["faster_whisper"], "WhisperModel", MostlySilenceWhisperModel)

    svc = TranscribeService(settings)
    result = svc.transcribe(BytesIO(b"\x00\x00"))

    assert result.text == "Toplantı kaydı başladı."
    assert [segment.text for segment in result.segments] == ["Toplantı kaydı başladı."]


def test_sync_transcribe_filters_low_confidence_segments(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.conftest import _FakeInfo, _FakeSeg

    class LowConfidenceWhisperModel:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def transcribe(self, _audio: object, **_kwargs: object):  # type: ignore[no-untyped-def]
            return [
                _FakeSeg(
                    0,
                    0.0,
                    1.0,
                    "Fena ya bizim bu şankeye damar.",
                    avg_logprob=settings.log_prob_threshold - 0.01,
                ),
                _FakeSeg(1, 1.0, 2.0, "Söylediklerimi doğru yazıyor."),
            ], _FakeInfo()

    monkeypatch.setattr(sys.modules["faster_whisper"], "WhisperModel", LowConfidenceWhisperModel)

    svc = TranscribeService(settings)
    result = svc.transcribe(BytesIO(b"\x00\x00"))

    assert result.text == "Söylediklerimi doğru yazıyor."
    assert [segment.text for segment in result.segments] == ["Söylediklerimi doğru yazıyor."]


def test_auto_language(settings: Settings) -> None:
    settings_auto = settings.model_copy(update={"language": "auto"})
    svc = TranscribeService(settings_auto)
    result = svc.transcribe(BytesIO(b"\x00"))
    # mock returns "tr" regardless; check it does not crash on auto
    assert result.language == "tr"


def test_get_service_singleton(settings: Settings) -> None:
    s1 = get_service(settings)
    s2 = get_service(settings)
    assert s1 is s2
