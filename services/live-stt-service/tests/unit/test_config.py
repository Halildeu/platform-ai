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
    assert s.worker_backend == "process"
    assert s.worker_max_workers == 1
    assert s.worker_kill_grace_sec == 2.0
    assert s.live_infer_interval_ms == 350
    assert s.live_window_sec == 2.0
    assert s.silence_commit_sec == 0.9
    assert s.min_infer_sec == 0.35


def test_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STT_MODEL_NAME", "large-v3-turbo")
    monkeypatch.setenv("STT_COMPUTE_TYPE", "float16")
    monkeypatch.setenv("STT_DEVICE", "cuda")
    monkeypatch.setenv("STT_LANGUAGE", "auto")
    monkeypatch.setenv("STT_BEAM_SIZE", "3")
    monkeypatch.setenv("STT_WORKER_BACKEND", "inline")
    monkeypatch.setenv("STT_WORKER_MAX_WORKERS", "2")
    monkeypatch.setenv("STT_WORKER_KILL_GRACE_SEC", "0.5")
    monkeypatch.setenv("STT_LIVE_INFER_INTERVAL_MS", "250")
    monkeypatch.setenv("STT_LIVE_WINDOW_SEC", "1.5")
    monkeypatch.setenv("STT_SILENCE_COMMIT_SEC", "0.4")
    s = cfg.Settings()
    assert s.model_name == "large-v3-turbo"
    assert s.compute_type == "float16"
    assert s.device == "cuda"
    assert s.language == "auto"
    assert s.beam_size == 3
    assert s.worker_backend == "inline"
    assert s.worker_max_workers == 2
    assert s.worker_kill_grace_sec == 0.5
    assert s.live_infer_interval_ms == 250
    assert s.live_window_sec == 1.5
    assert s.silence_commit_sec == 0.4


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


def test_worker_config_bounds() -> None:
    with pytest.raises(ValueError):
        cfg.Settings(worker_max_workers=0)
    with pytest.raises(ValueError):
        cfg.Settings(worker_max_workers=9)
    with pytest.raises(ValueError):
        cfg.Settings(worker_backend="thread")
    with pytest.raises(ValueError):
        cfg.Settings(worker_kill_grace_sec=-1.0)
    with pytest.raises(ValueError):
        cfg.Settings(worker_kill_grace_sec=31.0)


def test_stream_tuning_bounds_and_cross_field_guards() -> None:
    with pytest.raises(ValueError):
        cfg.Settings(live_infer_interval_ms=0)
    with pytest.raises(ValueError):
        cfg.Settings(silence_commit_sec=0.0)
    with pytest.raises(ValueError):
        cfg.Settings(min_speech_rms=0.01, silence_rms=0.02)
    with pytest.raises(ValueError):
        cfg.Settings(min_infer_sec=2.1, live_window_sec=2.0)
    with pytest.raises(ValueError):
        cfg.Settings(tail_overlap_sec=10.0, final_window_sec=10.0)


def test_settings_cached() -> None:
    s1 = cfg.get_settings()
    s2 = cfg.get_settings()
    assert s1 is s2
