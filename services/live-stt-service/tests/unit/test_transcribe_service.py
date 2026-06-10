"""TranscribeService unit tests — mocked Whisper."""

from __future__ import annotations

from io import BytesIO

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
