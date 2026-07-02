"""pytest fixtures.

faster-whisper module is mocked at import-time for unit tests to keep the suite
fast and CI-friendly. Integration tests (marked `integration`) load the real
model and require an audio fixture under `tests/fixtures/`.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Generator
from dataclasses import dataclass

import pytest


@dataclass
class _FakeSeg:
    id: int
    start: float
    end: float
    text: str
    avg_logprob: float = -0.3
    no_speech_prob: float = 0.05


@dataclass
class _FakeInfo:
    language: str = "tr"
    language_probability: float = 0.98
    duration: float = 2.5


class _FakeWhisperModel:
    """Drop-in mock for faster_whisper.WhisperModel — deterministic output."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.calls = 0

    def transcribe(
        self, _audio: object, **_kwargs: object
    ) -> tuple[list[_FakeSeg], _FakeInfo]:
        self.calls += 1
        language = _kwargs.get("language") or "tr"
        segments = [
            _FakeSeg(0, 0.0, 1.2, "Merhaba dünya."),
            _FakeSeg(1, 1.2, 2.5, "Toplantı başlıyor."),
        ]
        return segments, _FakeInfo(language=str(language))


@pytest.fixture(autouse=True)
def _mock_faster_whisper(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """Mock the faster_whisper module so import does not trigger model download."""
    fake_module = types.ModuleType("faster_whisper")
    fake_module.WhisperModel = _FakeWhisperModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_module)
    yield


@pytest.fixture
def reset_service_singleton() -> Generator[None, None, None]:
    """Reset the TranscribeService singleton between tests to avoid state leak."""
    from app.services import transcribe as svc_mod

    svc_mod._service = None
    yield
    svc_mod._service = None


@pytest.fixture
def settings_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force test-friendly settings (small model name, cpu, int8)."""
    monkeypatch.setenv("STT_MODEL_NAME", "tiny")
    monkeypatch.setenv("STT_DEVICE", "cpu")
    monkeypatch.setenv("STT_COMPUTE_TYPE", "int8")
    monkeypatch.setenv("STT_LANGUAGE", "tr")
    monkeypatch.setenv("STT_MAX_AUDIO_MB", "10")
    monkeypatch.setenv("STT_WORKER_BACKEND", "inline")
    # bust settings cache
    from app.core import config as cfg

    cfg._settings = None


@pytest.fixture
def client(settings_override, reset_service_singleton):  # type: ignore[no-untyped-def]
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c
