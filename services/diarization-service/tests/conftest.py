"""Shared test fixtures: reset module singletons so each test is isolated."""

from __future__ import annotations

import io
import wave
from collections.abc import Iterator

import pytest

import app.core.config as config_mod
import app.services.diarize as diarize_mod


@pytest.fixture(autouse=True)
def _reset_singletons() -> Iterator[None]:
    config_mod._settings = None
    diarize_mod._service = None
    yield
    config_mod._settings = None
    diarize_mod._service = None


def make_wav(seconds: float, rate: int = 16000) -> bytes:
    """Generate a mono 16-bit silent WAV of the given duration."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()
