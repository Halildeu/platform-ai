"""Config tests — env override + bounds + cache."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.core import config as cfg
from app.services.hallucination import (
    CONTEXTUAL_ARTIFACT_MAX_RMS,
    CONTEXTUAL_ARTIFACT_MIN_NO_SPEECH_PROB,
)

STREAM_MODEL_PINS = {
    "live_model_revision": "c" * 40,
    "live_model_sha256": "d" * 64,
    "live_model_tree_sha256": "1" * 64,
    "live_model_path": "/models/live",
    "final_model_revision": "e" * 40,
    "final_model_sha256": "f" * 64,
    "final_model_tree_sha256": "2" * 64,
    "final_model_path": "/models/final",
}

PRODUCTION_SPEECH_GATE = {
    "speech_gate_profile": "silero-balanced-v1",
    "stream_live_vad_filter": True,
    "stream_final_vad_filter": True,
    "stream_vad_threshold": 0.35,
    "stream_vad_min_speech_duration_ms": 100,
    "stream_vad_min_silence_duration_ms": 300,
    "stream_vad_speech_pad_ms": 100,
}


@pytest.fixture(autouse=True)
def reset_settings_cache() -> None:
    cfg._settings = None


def test_defaults() -> None:
    s = cfg.Settings()
    assert s.model_name == "medium"
    assert s.environment == "local"
    assert s.runtime_commit == "unversioned"
    assert s.model_revision == "unversioned"
    assert s.model_sha256 == ""
    assert s.model_path is None
    assert s.compute_type == "int8"
    assert s.device == "cpu"
    assert s.language == "tr"
    assert s.beam_size == 5
    assert s.vad_filter is True
    assert s.speech_gate_profile == "development-unpinned"
    assert s.speech_gate_rms_source == "source-baseline"
    assert s.stream_live_vad_filter is False
    assert s.stream_final_vad_filter is False
    assert s.stream_vad_threshold == 0.35
    assert s.stream_vad_min_speech_duration_ms == 100
    assert s.stream_vad_min_silence_duration_ms == 300
    assert s.stream_vad_speech_pad_ms == 100
    assert s.stream_final_worker_backend == "process"
    assert s.stream_live_worker_backend == "process"
    assert s.stream_live_timeout_sec == 5.0
    assert s.stream_model_load_timeout_sec == 180.0
    assert s.stream_transport_timeout_sec == 2.0
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
    monkeypatch.setenv("STT_STREAM_LIVE_VAD_FILTER", "true")
    monkeypatch.setenv("STT_STREAM_FINAL_VAD_FILTER", "true")
    monkeypatch.setenv("STT_STREAM_TRANSPORT_TIMEOUT_SEC", "0.5")
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
    assert s.stream_live_vad_filter is True
    assert s.stream_final_vad_filter is True
    assert s.stream_transport_timeout_sec == 0.5
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
    for field in ("silence_rms", "min_speech_rms"):
        with pytest.raises(ValueError):
            cfg.Settings(**{field: 0.00009})
        with pytest.raises(ValueError):
            cfg.Settings(**{field: 0.05001})
    for boundary in (0.0001, 0.05):
        bounded = cfg.Settings(silence_rms=boundary, min_speech_rms=boundary)
        assert bounded.silence_rms == boundary
        assert bounded.min_speech_rms == boundary
    with pytest.raises(ValueError):
        cfg.Settings(min_speech_rms=0.01, silence_rms=0.02)
    valid_rms_override = cfg.Settings(silence_rms=0.01, min_speech_rms=0.015)
    assert valid_rms_override.silence_rms == 0.01
    assert valid_rms_override.min_speech_rms == 0.015
    with pytest.raises(ValueError):
        cfg.Settings(min_infer_sec=2.1, live_window_sec=2.0)
    with pytest.raises(ValueError):
        cfg.Settings(tail_overlap_sec=10.0, final_window_sec=10.0)
    with pytest.raises(ValueError):
        cfg.Settings(stream_final_worker_backend="thread")
    with pytest.raises(ValueError):
        cfg.Settings(stream_live_worker_backend="thread")
    with pytest.raises(ValueError):
        cfg.Settings(stream_transport_timeout_sec=0.01)
    with pytest.raises(ValueError, match="terminal timeout budget must be <= 120 seconds"):
        cfg.Settings(
            stream_final_timeout_sec=60.0,
            worker_kill_grace_sec=30.0,
            stream_transport_timeout_sec=10.0,
            stream_preload_readiness_budget_sec=3600.0,
        )
    with pytest.raises(ValueError, match="must be process"):
        cfg.Settings(
            environment="staging",
            model_revision="a" * 40,
            model_sha256="b" * 64,
            model_tree_sha256="3" * 64,
            model_path="/models/immutable",
            **STREAM_MODEL_PINS,
            stream_final_worker_backend="inline",
        )
    with pytest.raises(ValueError, match="stream_live_worker_backend must be process"):
        cfg.Settings(
            environment="staging",
            model_revision="a" * 40,
            model_sha256="b" * 64,
            model_tree_sha256="3" * 64,
            model_path="/models/immutable",
            **STREAM_MODEL_PINS,
            stream_live_worker_backend="inline",
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
            model_tree_sha256="3" * 64,
        )
    with pytest.raises(ValueError, match="live_model_revision"):
        cfg.Settings(
            environment="production",
            model_revision="a" * 40,
            model_sha256="b" * 64,
            model_tree_sha256="3" * 64,
            model_path="/models/faster-whisper-medium",
        )
    with pytest.raises(ValueError, match="stream_preload_models must be enabled"):
        cfg.Settings(
            environment="production",
            model_revision="a" * 40,
            model_sha256="b" * 64,
            model_tree_sha256="3" * 64,
            model_path="/models/faster-whisper-medium",
            **STREAM_MODEL_PINS,
            runtime_commit="a" * 40,
        )
    with pytest.raises(ValueError, match="runtime_commit"):
        cfg.Settings(
            environment="production",
            model_revision="a" * 40,
            model_sha256="b" * 64,
            model_tree_sha256="3" * 64,
            model_path="/models/faster-whisper-medium",
            **STREAM_MODEL_PINS,
            stream_preload_models=True,
        )
    settings = cfg.Settings(
        environment="production",
        model_name="Systran/faster-whisper-medium",
        model_revision="a" * 40,
        model_sha256="sha256:" + "b" * 64,
        model_tree_sha256="sha256:" + "3" * 64,
        model_path="/models/faster-whisper-medium",
        **STREAM_MODEL_PINS,
        stream_preload_models=True,
        runtime_commit="a" * 40,
        **PRODUCTION_SPEECH_GATE,
    )
    assert settings.model_revision == "a" * 40


def test_preload_worst_case_must_fit_the_declared_readiness_budget() -> None:
    with pytest.raises(ValueError, match="preload worst-case timeout"):
        cfg.Settings(
            stream_preload_max_attempts=5,
            stream_model_load_timeout_sec=600,
            worker_kill_grace_sec=30,
            stream_preload_retry_base_sec=30,
            stream_preload_readiness_budget_sec=780,
        )


def test_preload_budget_counts_exponential_retry_waits() -> None:
    with pytest.raises(ValueError, match="preload worst-case timeout"):
        cfg.Settings(
            stream_preload_max_attempts=3,
            stream_model_load_timeout_sec=1,
            worker_kill_grace_sec=1,
            stream_preload_retry_base_sec=10,
            stream_preload_readiness_budget_sec=60,
        )


def test_production_runtime_profile_is_fail_closed() -> None:
    base = {
        "environment": "production",
        "runtime_commit": "a" * 40,
        "model_revision": "a" * 40,
        "model_sha256": "b" * 64,
        "model_tree_sha256": "3" * 64,
        "model_path": "/models/faster-whisper-medium",
        **STREAM_MODEL_PINS,
        "stream_preload_models": True,
        **PRODUCTION_SPEECH_GATE,
    }
    with pytest.raises(ValueError, match="device must be cpu"):
        cfg.Settings(**base, device="cuda")
    with pytest.raises(ValueError, match="live_device must be cuda"):
        cfg.Settings(**base, live_device="cpu")
    with pytest.raises(ValueError, match="final_compute_type must be float16"):
        cfg.Settings(**base, final_compute_type="int8")
    with pytest.raises(ValueError, match="speech_gate_profile"):
        cfg.Settings(**{**base, "speech_gate_profile": "development-unpinned"})
    # Draft-lane VAD is now a policy choice: production must ACCEPT
    # stream_live_vad_filter=False (gitops#3419 draft starvation) while the
    # final-lane VAD invariant stays fail-closed.
    accepted = cfg.Settings(**{**base, "stream_live_vad_filter": False})
    assert accepted.stream_live_vad_filter is False
    with pytest.raises(ValueError, match="stream_final_vad_filter"):
        cfg.Settings(**{**base, "stream_final_vad_filter": False})
    with pytest.raises(ValueError, match="stream_vad_threshold"):
        cfg.Settings(**{**base, "stream_vad_threshold": 0.5})
    with pytest.raises(ValueError, match="stream_vad_min_silence_duration_ms"):
        cfg.Settings(**{**base, "stream_vad_min_silence_duration_ms": 2000})
    with pytest.raises(ValueError, match="live_window_sec"):
        cfg.Settings(**{**base, "live_window_sec": 0.1, "min_infer_sec": 0.05})
    with pytest.raises(ValueError, match="live_infer_interval_ms"):
        cfg.Settings(**{**base, "live_infer_interval_ms": 100})
    for field, value in (
        ("forced_commit_sec", 60.0),
        ("silence_commit_sec", 5.0),
        ("tail_overlap_sec", 5.0),
    ):
        with pytest.raises(ValueError, match=field):
            cfg.Settings(**{**base, field: value})


def test_source_controlled_speech_gate_contract_does_not_drift() -> None:
    repo_root = Path(__file__).resolve().parents[4]
    contract = (repo_root / "deploy/gpu-host/live-stt-runtime-contract.ps1").read_text()
    launcher = (repo_root / "deploy/gpu-host/start-live-stt.ps1").read_text()
    provisioner = (repo_root / "deploy/gpu-host/configure-live-stt.ps1").read_text()
    env_example = (repo_root / "deploy/gpu-host/live-stt.env.example").read_text()
    readme = (repo_root / "services/live-stt-service/README.md").read_text()

    def contract_value(name: str) -> str:
        match = re.search(rf"\$script:{name}\s*=\s*\"?([^\"\r\n]+)\"?", contract)
        assert match is not None, name
        return match.group(1).strip()

    settings = cfg.Settings()
    expected = {
        "LiveSttSpeechGateProfile": settings.speech_gate_profile.replace(
            "development-unpinned", "silero-balanced-v1"
        ),
        "LiveSttSilenceRms": str(settings.silence_rms),
        "LiveSttMinSpeechRms": str(settings.min_speech_rms),
        "LiveSttStreamVadThreshold": str(settings.stream_vad_threshold),
        "LiveSttStreamVadMinSpeechDurationMs": str(
            settings.stream_vad_min_speech_duration_ms
        ),
        "LiveSttStreamVadMinSilenceDurationMs": str(
            settings.stream_vad_min_silence_duration_ms
        ),
        "LiveSttStreamVadSpeechPadMs": str(settings.stream_vad_speech_pad_ms),
        "LiveSttLiveInferIntervalMs": str(settings.live_infer_interval_ms),
        "LiveSttLiveWindowSec": str(settings.live_window_sec),
        "LiveSttFinalWindowSec": str(settings.final_window_sec),
        "LiveSttForcedCommitSec": str(settings.forced_commit_sec),
        "LiveSttSilenceCommitSec": str(settings.silence_commit_sec),
        "LiveSttTailOverlapSec": str(settings.tail_overlap_sec),
        "LiveSttMinInferSec": str(settings.min_infer_sec),
        "LiveSttContextualArtifactMaxRms": str(CONTEXTUAL_ARTIFACT_MAX_RMS),
        "LiveSttContextualArtifactMinNoSpeechProb": str(
            CONTEXTUAL_ARTIFACT_MIN_NO_SPEECH_PROB
        ),
    }
    for name, value in expected.items():
        assert contract_value(name) == value
        if not name.startswith("LiveSttContextualArtifact"):
            assert f"$script:{name}" in launcher

    assert "STT_SILENCE_RMS=0.0005" in env_example
    assert "STT_MIN_SPEECH_RMS=0.0005" in env_example
    assert "`0.0005` / `0.0005`" in readme
    assert '-InitialDefault "0.0005"' not in provisioner


def test_model_sha256_rejects_malformed_value() -> None:
    with pytest.raises(ValueError, match="model_sha256"):
        cfg.Settings(model_sha256="sha256:not-a-digest")
    with pytest.raises(ValueError, match="model_sha256"):
        cfg.Settings(model_sha256="A" * 64)
    with pytest.raises(ValueError, match="live_model_sha256"):
        cfg.Settings(live_model_sha256="sha256:not-a-digest")
    with pytest.raises(ValueError, match="final_model_sha256"):
        cfg.Settings(final_model_sha256="A" * 64)
