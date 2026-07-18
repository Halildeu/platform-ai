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
    assert s.environment == "local"
    assert s.model_revision == "unversioned"
    assert s.model_sha256 == ""
    assert s.model_path is None
    assert s.compute_type == "int8"
    assert s.device == "cpu"
    assert s.language == "tr"
    assert s.beam_size == 5
    assert s.vad_filter is True
    assert s.stream_final_vad_filter is False
    assert s.stream_final_worker_backend == "process"
    assert s.stream_model_load_timeout_sec == 180.0
    assert s.worker_backend == "process"
    assert s.worker_max_workers == 1
    assert s.worker_kill_grace_sec == 2.0
    assert s.live_beam_size == 1
    assert s.final_beam_size == 1
    assert s.live_infer_interval_ms == 700
    assert s.live_window_sec == 2.0
    assert s.final_window_sec == 6.0
    assert s.forced_commit_sec == 5.0
    assert s.silence_commit_sec == 0.7
    assert s.silence_rms == 0.0005
    assert s.min_speech_rms == 0.0005
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
    monkeypatch.setenv("STT_LIVE_BEAM_SIZE", "2")
    monkeypatch.setenv("STT_FINAL_BEAM_SIZE", "4")
    monkeypatch.setenv("STT_STREAM_FINAL_VAD_FILTER", "true")
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
    assert s.live_beam_size == 2
    assert s.final_beam_size == 4
    assert s.stream_final_vad_filter is True
    assert s.live_infer_interval_ms == 250
    assert s.live_window_sec == 1.5
    assert s.silence_commit_sec == 0.4


def test_beam_size_bounds() -> None:
    with pytest.raises(ValueError):
        cfg.Settings(beam_size=0)
    with pytest.raises(ValueError):
        cfg.Settings(beam_size=11)
    with pytest.raises(ValueError):
        cfg.Settings(live_beam_size=0)
    with pytest.raises(ValueError):
        cfg.Settings(live_beam_size=11)
    with pytest.raises(ValueError):
        cfg.Settings(final_beam_size=0)
    with pytest.raises(ValueError):
        cfg.Settings(final_beam_size=11)


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
    with pytest.raises(ValueError):
        cfg.Settings(stream_final_worker_backend="thread")
    with pytest.raises(ValueError, match="must be process"):
        cfg.Settings(
            environment="staging",
            stream_final_worker_backend="inline",
        )


def test_settings_cached() -> None:
    s1 = cfg.get_settings()
    s2 = cfg.get_settings()
    assert s1 is s2


def test_production_requires_content_addressed_local_model() -> None:
    with pytest.raises(ValueError, match="model_revision"):
        cfg.Settings(environment="production")
    with pytest.raises(ValueError, match="model_sha256"):
        cfg.Settings(environment="production", model_revision="a" * 40)
    with pytest.raises(ValueError, match="model_path"):
        cfg.Settings(
            environment="production",
            model_revision="a" * 40,
            model_sha256="b" * 64,
        )
    settings = cfg.Settings(
        environment="production",
        model_name="Systran/faster-whisper-medium",
        model_revision="a" * 40,
        model_sha256="sha256:" + "b" * 64,
        model_path="/models/faster-whisper-medium",
    )
    assert settings.model_revision == "a" * 40


def test_model_sha256_rejects_malformed_value() -> None:
    with pytest.raises(ValueError, match="model_sha256"):
        cfg.Settings(model_sha256="sha256:not-a-digest")
    with pytest.raises(ValueError, match="model_sha256"):
        cfg.Settings(model_sha256="A" * 64)
