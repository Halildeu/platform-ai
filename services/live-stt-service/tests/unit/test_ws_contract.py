"""#62 producer-side contract gate for /ws/stream events.

Web/mobile clients consume these events; this test validates every event shape
against docs/contracts/ws-stream-events.schema.json so contract drift fails CI
instead of surfacing in production.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from app.core.config import Settings, get_settings
from app.main import app
from app.services import streaming_models

SCHEMA_PATH = (
    Path(__file__).resolve().parents[4] / "docs" / "contracts" / "ws-stream-events.schema.json"
)
VALIDATOR = Draft202012Validator(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))


@pytest.fixture(autouse=True)
def clear_dependency_overrides() -> Iterator[None]:
    app.dependency_overrides.clear()
    yield
    app.dependency_overrides.clear()


def assert_valid(event: dict[str, Any]) -> None:
    errors = sorted(VALIDATOR.iter_errors(event), key=str)
    assert not errors, f"contract violation for {event!r}: {[e.message for e in errors]}"


def test_schema_file_is_valid_jsonschema() -> None:
    Draft202012Validator.check_schema(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))


def test_handshake_events_match_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real WS handshake: loading + loading + ready, each schema-valid."""
    monkeypatch.setattr(streaming_models.DirectWhisperService, "ensure_model", lambda self: None)
    with TestClient(app) as client, client.websocket_connect("/ws/stream") as ws:
        first = ws.receive_json()
        second = ws.receive_json()
        ready = ws.receive_json()

    for event in (first, second, ready):
        assert_valid(event)
    assert [first["type"], second["type"], ready["type"]] == ["loading", "loading", "ready"]
    assert first["stage"] == "live_model"
    assert second["stage"] == "final_model"


def test_partial_and_final_payload_shapes_match_contract() -> None:
    """Mirror of the exact payloads stream.py emits (keys must stay in sync)."""
    partial = {
        "type": "partial",
        "seq": 0,
        "confirmed": "",
        "tentative": "merhaba",
        "elapsed_ms": 250,
        "rms": 0.01234,
        "source": "medium",
    }
    final = {
        "type": "final",
        "seq": 0,
        "text": "Merhaba, toplanti basliyor.",
        "reason": "silence",
        "elapsed_ms": 600,
        "rms": 0.01234,
    }
    error = {"type": "error", "msg": "RuntimeError"}
    for event in (partial, final, error):
        assert_valid(event)


def test_contract_rejects_unknown_event_type() -> None:
    errors = list(VALIDATOR.iter_errors({"type": "telemetry", "x": 1}))
    assert errors, "schema must reject unknown event types (drift gate)"


def _speech_frame() -> bytes:
    return (np.ones(1024, dtype=np.float32) * 0.05).tobytes()


def _speech_frame_with_level(level: float) -> bytes:
    return (np.ones(1024, dtype=np.float32) * level).tobytes()


def _silence_frame() -> bytes:
    return np.zeros(1024, dtype=np.float32).tobytes()


def _patch_fast_stream_timing(
    monkeypatch: pytest.MonkeyPatch,
    *,
    forced_commit_sec: float = 60.0,
    silence_commit_sec: float = 0.1,
    tail_overlap_sec: float = 0.0,
) -> None:
    settings = Settings(
        live_infer_interval_ms=1,
        live_window_sec=1.0,
        final_window_sec=5.0,
        forced_commit_sec=forced_commit_sec,
        silence_commit_sec=silence_commit_sec,
        tail_overlap_sec=tail_overlap_sec,
        silence_rms=0.001,
        min_speech_rms=0.001,
        min_infer_sec=0.01,
    )
    app.dependency_overrides[get_settings] = lambda: settings
    monkeypatch.setattr(streaming_models.DirectWhisperService, "ensure_model", lambda self: None)


def test_stream_emits_same_seq_word_progressive_partials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real WS behavior: repeated live partials update one segment id."""
    _patch_fast_stream_timing(monkeypatch)
    live_drafts = iter(["Merhaba", "Merhaba nasılsın"])

    def fake_transcribe(
        self: streaming_models.DirectWhisperService,
        _audio: np.ndarray[tuple[int, ...], np.dtype[np.float32]],
        vad: bool,
    ) -> str:
        return "Merhaba nasılsın." if vad else next(live_drafts)

    monkeypatch.setattr(streaming_models.DirectWhisperService, "transcribe_array", fake_transcribe)

    with TestClient(app) as client, client.websocket_connect("/ws/stream") as ws:
        for _ in range(3):
            assert_valid(ws.receive_json())

        ws.send_bytes(_speech_frame())
        first = ws.receive_json()
        time.sleep(0.002)
        ws.send_bytes(_speech_frame())
        second = ws.receive_json()

    for event in (first, second):
        assert_valid(event)
        assert event["type"] == "partial"
        assert event["seq"] == 0
    assert first["tentative"] == "Merhaba"
    assert second["tentative"] == "Merhaba nasılsın"


def test_stream_default_gate_accepts_quiet_desktop_microphone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Desktop/WebAudio speech around RMS 0.002 must not be treated as silence."""

    settings = Settings(
        live_infer_interval_ms=1,
        final_window_sec=5.0,
        forced_commit_sec=60.0,
        silence_commit_sec=0.1,
    )
    app.dependency_overrides[get_settings] = lambda: settings
    monkeypatch.setattr(streaming_models.DirectWhisperService, "ensure_model", lambda self: None)

    def fake_transcribe(
        self: streaming_models.DirectWhisperService,
        _audio: np.ndarray[tuple[int, ...], np.dtype[np.float32]],
        vad: bool,
    ) -> str:
        return "Sessiz olmayan masaüstü konuşması" if not vad else "Sessiz olmayan konuşma."

    monkeypatch.setattr(streaming_models.DirectWhisperService, "transcribe_array", fake_transcribe)

    with TestClient(app) as client, client.websocket_connect("/ws/stream") as ws:
        for _ in range(3):
            assert_valid(ws.receive_json())

        for _ in range(6):
            ws.send_bytes(_speech_frame_with_level(0.002))
            time.sleep(0.002)
        partial = ws.receive_json()

    assert_valid(partial)
    assert partial["type"] == "partial"
    assert partial["seq"] == 0
    assert partial["tentative"] == "Sessiz olmayan masaüstü konuşması"
    assert partial["rms"] == pytest.approx(0.002, abs=0.0001)


def test_stream_keeps_receiving_audio_while_live_model_is_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slow live inference must not stop the WebSocket receive loop."""
    _patch_fast_stream_timing(
        monkeypatch,
        forced_commit_sec=0.16,
        silence_commit_sec=5.0,
    )
    final_sample_counts: list[int] = []

    def fake_transcribe(
        self: streaming_models.DirectWhisperService,
        audio: np.ndarray[tuple[int, ...], np.dtype[np.float32]],
        vad: bool,
    ) -> str:
        if vad:
            final_sample_counts.append(int(audio.size))
            return "Canlı final metin."
        time.sleep(0.08)
        return "Canlı taslak"

    monkeypatch.setattr(streaming_models.DirectWhisperService, "transcribe_array", fake_transcribe)

    with TestClient(app) as client, client.websocket_connect("/ws/stream") as ws:
        for _ in range(3):
            assert_valid(ws.receive_json())

        ws.send_bytes(_speech_frame())
        time.sleep(0.06)
        for _ in range(8):
            ws.send_bytes(_speech_frame())
            time.sleep(0.005)

        events = [ws.receive_json(), ws.receive_json()]

    for event in events:
        assert_valid(event)
    assert events[0]["type"] == "partial"
    assert events[1]["type"] == "final"
    assert final_sample_counts
    assert final_sample_counts[0] >= 8 * 1024


def test_stream_preserves_audio_received_while_slow_final_clips_buffer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audio received during a slow final pass must become the next segment."""

    settings = Settings(
        live_infer_interval_ms=5_000,
        live_window_sec=1.0,
        final_window_sec=1.0,
        forced_commit_sec=0.1,
        silence_commit_sec=5.0,
        tail_overlap_sec=0.0,
        silence_rms=0.001,
        min_speech_rms=0.001,
        min_infer_sec=0.01,
    )
    app.dependency_overrides[get_settings] = lambda: settings
    monkeypatch.setattr(streaming_models.DirectWhisperService, "ensure_model", lambda self: None)

    final_sample_counts: list[int] = []

    def fake_transcribe(
        self: streaming_models.DirectWhisperService,
        audio: np.ndarray[tuple[int, ...], np.dtype[np.float32]],
        vad: bool,
    ) -> str:
        if vad:
            final_sample_counts.append(int(audio.size))
            if len(final_sample_counts) == 1:
                time.sleep(0.12)
            return f"Final {len(final_sample_counts)}."
        return ""

    monkeypatch.setattr(streaming_models.DirectWhisperService, "transcribe_array", fake_transcribe)

    with TestClient(app) as client, client.websocket_connect("/ws/stream") as ws:
        for _ in range(3):
            assert_valid(ws.receive_json())

        for _ in range(20):
            ws.send_bytes(_speech_frame())
        time.sleep(0.12)
        ws.send_bytes(_speech_frame())

        for _ in range(8):
            ws.send_bytes(_speech_frame())
            time.sleep(0.005)

        first_final = ws.receive_json()
        ws.send_bytes(_speech_frame())
        time.sleep(0.12)
        second_final = ws.receive_json()

    for event in (first_final, second_final):
        assert_valid(event)
        assert event["type"] == "final"
    assert len(final_sample_counts) >= 2
    assert final_sample_counts[1] >= 8 * 1024


def test_stream_appends_growing_no_overlap_live_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later rolling window must not erase already displayed words."""
    _patch_fast_stream_timing(monkeypatch)
    live_drafts = iter(["Merhaba sesim geliyor mu", "bir sürü eksik var yine"])

    def fake_transcribe(
        self: streaming_models.DirectWhisperService,
        _audio: np.ndarray[tuple[int, ...], np.dtype[np.float32]],
        vad: bool,
    ) -> str:
        return "Merhaba sesim geliyor mu bir sürü eksik var yine." if vad else next(live_drafts)

    monkeypatch.setattr(streaming_models.DirectWhisperService, "transcribe_array", fake_transcribe)

    with TestClient(app) as client, client.websocket_connect("/ws/stream") as ws:
        for _ in range(3):
            assert_valid(ws.receive_json())

        ws.send_bytes(_speech_frame())
        first = ws.receive_json()
        time.sleep(0.002)
        ws.send_bytes(_speech_frame())
        second = ws.receive_json()

    for event in (first, second):
        assert_valid(event)
        assert event["type"] == "partial"
        assert event["seq"] == 0
    assert first["tentative"] == "Merhaba sesim geliyor mu"
    assert second["tentative"] == "Merhaba sesim geliyor mu bir sürü eksik var yine"


def test_stream_keeps_short_stable_draft_over_unrelated_short_final(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Short final corrections should not replace a stable draft with noise."""
    _patch_fast_stream_timing(monkeypatch)

    def fake_transcribe(
        self: streaming_models.DirectWhisperService,
        _audio: np.ndarray[tuple[int, ...], np.dtype[np.float32]],
        vad: bool,
    ) -> str:
        return "Neroba" if vad else "Merhaba"

    monkeypatch.setattr(streaming_models.DirectWhisperService, "transcribe_array", fake_transcribe)

    with TestClient(app) as client, client.websocket_connect("/ws/stream") as ws:
        for _ in range(3):
            assert_valid(ws.receive_json())

        ws.send_bytes(_speech_frame())
        partial = ws.receive_json()
        time.sleep(0.11)
        ws.send_bytes(_silence_frame())
        final = ws.receive_json()

    assert_valid(partial)
    assert partial["type"] == "partial"
    assert partial["tentative"] == "Merhaba"

    assert_valid(final)
    assert final["type"] == "final"
    assert final["text"] == "Merhaba"


def test_stream_commits_final_on_speech_ending_silence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Speech-ending silence should not wait for the long forced-commit age."""
    _patch_fast_stream_timing(monkeypatch)

    def fake_transcribe(
        self: streaming_models.DirectWhisperService,
        _audio: np.ndarray[tuple[int, ...], np.dtype[np.float32]],
        vad: bool,
    ) -> str:
        return "Merhaba nasılsın." if vad else "Merhaba nasılsın"

    monkeypatch.setattr(streaming_models.DirectWhisperService, "transcribe_array", fake_transcribe)

    with TestClient(app) as client, client.websocket_connect("/ws/stream") as ws:
        for _ in range(3):
            assert_valid(ws.receive_json())

        ws.send_bytes(_speech_frame())
        partial = ws.receive_json()
        time.sleep(0.11)
        ws.send_bytes(_silence_frame())
        final = ws.receive_json()

    assert_valid(partial)
    assert partial["type"] == "partial"
    assert partial["seq"] == 0
    assert partial["tentative"] == "Merhaba nasılsın"

    assert_valid(final)
    assert final["type"] == "final"
    assert final["seq"] == 0
    assert final["reason"] == "silence"
    assert final["text"] == "Merhaba nasılsın."


def test_stream_forced_commit_still_emits_final(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forced finalization remains the safety net for long active speech."""
    _patch_fast_stream_timing(
        monkeypatch,
        forced_commit_sec=0.1,
        silence_commit_sec=5.0,
    )

    def fake_transcribe(
        self: streaming_models.DirectWhisperService,
        _audio: np.ndarray[tuple[int, ...], np.dtype[np.float32]],
        vad: bool,
    ) -> str:
        return "Uzun konuşma final." if vad else "Uzun konuşma"

    monkeypatch.setattr(streaming_models.DirectWhisperService, "transcribe_array", fake_transcribe)

    with TestClient(app) as client, client.websocket_connect("/ws/stream") as ws:
        for _ in range(3):
            assert_valid(ws.receive_json())

        ws.send_bytes(_speech_frame())
        partial = ws.receive_json()
        time.sleep(0.11)
        ws.send_bytes(_speech_frame())
        final = ws.receive_json()

    assert_valid(partial)
    assert partial["type"] == "partial"
    assert partial["seq"] == 0

    assert_valid(final)
    assert final["type"] == "final"
    assert final["seq"] == 0
    assert final["reason"] == "forced"
    assert final["text"] == "Uzun konuşma final."


def test_stream_silence_commit_does_not_carry_tail_into_next_utterance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Speech-ending silence is a boundary; next utterance must not inherit old tail."""
    _patch_fast_stream_timing(monkeypatch, tail_overlap_sec=0.01)
    final_first_samples: list[float] = []

    def fake_transcribe(
        self: streaming_models.DirectWhisperService,
        audio: np.ndarray[tuple[int, ...], np.dtype[np.float32]],
        vad: bool,
    ) -> str:
        if vad:
            final_first_samples.append(float(audio[0]))
            return f"Final {len(final_first_samples)}."
        return "Taslak"

    monkeypatch.setattr(streaming_models.DirectWhisperService, "transcribe_array", fake_transcribe)

    with TestClient(app) as client, client.websocket_connect("/ws/stream") as ws:
        for _ in range(3):
            assert_valid(ws.receive_json())

        ws.send_bytes(_speech_frame_with_level(0.05))
        first_partial = ws.receive_json()
        time.sleep(0.11)
        ws.send_bytes(_silence_frame())
        first_final = ws.receive_json()

        ws.send_bytes(_speech_frame_with_level(0.07))
        second_partial = ws.receive_json()
        time.sleep(0.11)
        ws.send_bytes(_silence_frame())
        second_final = ws.receive_json()

    for event in (first_partial, first_final, second_partial, second_final):
        assert_valid(event)
    assert first_final["type"] == "final"
    assert second_final["type"] == "final"
    assert final_first_samples == pytest.approx([0.05, 0.07])
