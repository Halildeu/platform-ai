"""Config tests — env override + bounds + cache."""

from __future__ import annotations

import pytest

from app.core import config as cfg


@pytest.fixture(autouse=True)
def reset_settings_cache() -> None:
    cfg._settings = None


def test_defaults() -> None:
    s = cfg.Settings()
    assert s.model_name == "medium"
    assert s.compute_type == "int8"
    assert s.device == "cpu"
    assert s.language == "tr"
    assert s.beam_size == 5
    assert s.vad_filter is True


def test_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STT_MODEL_NAME", "large-v3-turbo")
    monkeypatch.setenv("STT_COMPUTE_TYPE", "float16")
    monkeypatch.setenv("STT_DEVICE", "cuda")
    monkeypatch.setenv("STT_LANGUAGE", "auto")
    monkeypatch.setenv("STT_BEAM_SIZE", "3")
    s = cfg.Settings()
    assert s.model_name == "large-v3-turbo"
    assert s.compute_type == "float16"
    assert s.device == "cuda"
    assert s.language == "auto"
    assert s.beam_size == 3


def test_beam_size_bounds() -> None:
    with pytest.raises(ValueError):
        cfg.Settings(beam_size=0)
    with pytest.raises(ValueError):
        cfg.Settings(beam_size=11)


def test_max_audio_mb_bounds() -> None:
    with pytest.raises(ValueError):
        cfg.Settings(max_audio_mb=0)
    with pytest.raises(ValueError):
        cfg.Settings(max_audio_mb=501)


def test_settings_cached() -> None:
    s1 = cfg.get_settings()
    s2 = cfg.get_settings()
    assert s1 is s2
