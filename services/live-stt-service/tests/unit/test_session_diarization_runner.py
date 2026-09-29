"""#3746 AI-D3 — supervised diarization runner protocol tests.

The real pyannote child needs the GPU host (D5 measurement); these tests pin
the *supervision* contract with injectable spawn-safe stub children: done /
defer / fail replies, silent-exit, malformed replies, and the
timeout->terminate->kill path. No torch import anywhere.
"""

from __future__ import annotations

import time

import pytest

from app.services.session_attribution import DiarTurn
from app.services.session_diarization_runner import (
    DiarizationStatus,
    RunnerConfig,
    run_session_diarization,
)

WAV = b"RIFF" + b"\x00" * 60  # opaque bytes; the runner never parses audio


def make_config(**overrides: object) -> RunnerConfig:
    base: dict[str, object] = {
        "model_name": "pyannote/speaker-diarization-3.1",
        "model_revision": "84fd25912480287da0247647c3d2b4853cb3ee5d",
        "hf_token": "unused-in-tests",
        "max_speakers": 10,
        "device": "cpu",
        "hard_timeout_sec": 15.0,
        "kill_grace_sec": 2.0,
    }
    base.update(overrides)
    return RunnerConfig(**base)  # type: ignore[arg-type]


# ── spawn-safe stub children (module-level: spawn pickles by reference) ──


def child_done(conn, wav_bytes: bytes, config: RunnerConfig) -> None:
    conn.send(("done", config.model_revision, [("SPEAKER_00", 0, 1000), ("S1", 500, 900)]))
    conn.close()


def child_defer(conn, wav_bytes: bytes, config: RunnerConfig) -> None:
    conn.send(("deferred_vram", "free_mb=812", ""))
    conn.close()


def child_fail(conn, wav_bytes: bytes, config: RunnerConfig) -> None:
    conn.send(("failed", "RuntimeError", ""))
    conn.close()


def child_silent_exit(conn, wav_bytes: bytes, config: RunnerConfig) -> None:
    conn.close()


def child_garbage(conn, wav_bytes: bytes, config: RunnerConfig) -> None:
    conn.send(("done", "rev", [("SPEAKER_00", "not-an-int", 1000)]))
    conn.close()


def child_hang(conn, wav_bytes: bytes, config: RunnerConfig) -> None:
    time.sleep(60)


def test_done_reply_maps_to_turns() -> None:
    outcome = run_session_diarization(WAV, make_config(), child_target=child_done)
    assert outcome.status is DiarizationStatus.DONE
    assert outcome.turns == [DiarTurn("SPEAKER_00", 0, 1000), DiarTurn("S1", 500, 900)]
    assert outcome.model_revision == make_config().model_revision
    assert outcome.elapsed_ms >= 0


def test_vram_deferral_is_reported_without_turns() -> None:
    outcome = run_session_diarization(WAV, make_config(), child_target=child_defer)
    assert outcome.status is DiarizationStatus.DEFERRED_VRAM
    assert outcome.turns == []
    assert "free_mb" in outcome.detail


def test_child_failure_is_contained() -> None:
    outcome = run_session_diarization(WAV, make_config(), child_target=child_fail)
    assert outcome.status is DiarizationStatus.FAILED
    assert outcome.detail == "RuntimeError"


def test_silent_child_exit_is_a_protocol_error() -> None:
    outcome = run_session_diarization(WAV, make_config(), child_target=child_silent_exit)
    assert outcome.status is DiarizationStatus.FAILED
    assert outcome.detail == "child_protocol_error"


def test_malformed_turn_shape_is_a_failure_not_a_crash() -> None:
    outcome = run_session_diarization(WAV, make_config(), child_target=child_garbage)
    assert outcome.status is DiarizationStatus.FAILED
    assert outcome.detail == "child_turn_shape_error"


def test_hanging_child_is_terminated() -> None:
    outcome = run_session_diarization(
        WAV,
        make_config(hard_timeout_sec=1.5, kill_grace_sec=1.0),
        child_target=child_hang,
    )
    assert outcome.status is DiarizationStatus.TIMEOUT_KILLED
    assert outcome.detail == "hard_timeout"
    # The whole call must return promptly after the cap, not after the hang.
    assert outcome.elapsed_ms < 10_000


def test_empty_audio_rejected() -> None:
    with pytest.raises(ValueError):
        run_session_diarization(b"", make_config(), child_target=child_done)


def test_config_bounds() -> None:
    with pytest.raises(ValueError):
        make_config(max_speakers=0)
    with pytest.raises(ValueError):
        make_config(model_revision="")
    with pytest.raises(ValueError):
        make_config(hard_timeout_sec=0)
