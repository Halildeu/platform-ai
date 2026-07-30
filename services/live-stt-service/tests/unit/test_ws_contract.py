"""#62 producer-side contract gate for /ws/stream events.

Web/mobile clients consume these events; this test validates every event shape
against docs/contracts/ws-stream-events.schema.json so contract drift fails CI
instead of surfacing in production.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import wave
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import numpy as np
import pytest
from fastapi.testclient import TestClient
from faster_whisper.vad import VadOptions, collect_chunks, get_speech_timestamps
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from starlette.websockets import WebSocket, WebSocketDisconnect

from app.core import config as config_module
from app.core.config import Settings, get_settings
from app.main import app
from app.services import streaming_models

SCHEMA_PATH = (
    Path(__file__).resolve().parents[4] / "docs" / "contracts" / "ws-stream-events.schema.json"
)
VALIDATOR = Draft202012Validator(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))
STREAM_PATH = "/ws/stream?protocol=source-ranges-v1"
FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures"
SILERO_FIXTURES = json.loads(
    (FIXTURE_DIR / "silero-gate-fixtures.json").read_text(encoding="utf-8")
)


@pytest.fixture(autouse=True)
def clear_dependency_overrides(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("STT_STREAM_LIVE_WORKER_BACKEND", "inline")
    monkeypatch.setenv("STT_STREAM_FINAL_WORKER_BACKEND", "inline")
    config_module._settings = None
    app.dependency_overrides.clear()
    yield
    app.dependency_overrides.clear()
    config_module._settings = None


def assert_valid(event: dict[str, Any]) -> None:
    errors = sorted(VALIDATOR.iter_errors(event), key=str)
    assert not errors, f"contract violation for {event!r}: {[e.message for e in errors]}"


def receive_terminal_ack(ws: Any) -> dict[str, Any]:
    """Consume any in-flight partials that were emitted before EOF was acknowledged."""
    for _ in range(4):
        event = ws.receive_json()
        assert_valid(event)
        if event["type"] == "eof_ack":
            return cast(dict[str, Any], event)
        assert event["type"] == "partial"
    raise AssertionError("eof_ack was not emitted within the bounded terminal sequence")


def test_schema_file_is_valid_jsonschema() -> None:
    Draft202012Validator.check_schema(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))


def test_handshake_without_source_range_protocol_fails_closed() -> None:
    with TestClient(app) as client, client.websocket_connect("/ws/stream") as ws:
        error = ws.receive_json()
        assert_valid(error)
        assert error == {"type": "error", "msg": "protocol_required"}
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_handshake_events_match_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real WS handshake: loading + loading + ready, each schema-valid."""
    monkeypatch.setattr(streaming_models.DirectWhisperService, "ensure_model", lambda self: None)
    with TestClient(app) as client, client.websocket_connect(STREAM_PATH) as ws:
        first = ws.receive_json()
        second = ws.receive_json()
        ready = ws.receive_json()

    for event in (first, second, ready):
        assert_valid(event)
    assert [first["type"], second["type"], ready["type"]] == ["loading", "loading", "ready"]
    assert first["stage"] == "live_model"
    assert second["stage"] == "final_model"
    assert ready["partial_mode"] == "stable-v1"
    assert ready["protocol"] == "source-ranges-v1"
    assert ready["capabilities"] == ["eof", "source-ranges-v1", "context-v1"]
    assert ready["supports_eof"] is True
    assert ready["terminal_timeout_ms"] == 46_000


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
        "source_start_sample": 0,
        "source_end_sample": 16000,
    }
    error = {"type": "error", "msg": "RuntimeError"}
    eof_ack = {"type": "eof_ack"}
    drained = {"type": "drained"}
    for event in (partial, final, eof_ack, drained, error):
        assert_valid(event)


def test_eof_without_audio_emits_ack_then_drained(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_fast_stream_timing(monkeypatch)

    with TestClient(app) as client, client.websocket_connect(STREAM_PATH) as ws:
        for _ in range(3):
            assert_valid(ws.receive_json())

        ws.send_text('{"type":"eof"}')
        eof_ack = receive_terminal_ack(ws)
        drained = ws.receive_json()

    assert_valid(eof_ack)
    assert_valid(drained)
    assert [eof_ack["type"], drained["type"]] == ["eof_ack", "drained"]


def test_context_control_before_audio_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_fast_stream_timing(monkeypatch)

    with TestClient(app) as client, client.websocket_connect(STREAM_PATH) as ws:
        for _ in range(3):
            assert_valid(ws.receive_json())

        ws.send_text('{"type":"context","terms":["Çağrı Öztürk","Proje-24"]}')
        ws.send_text('{"type":"eof"}')
        eof_ack = receive_terminal_ack(ws)
        drained = ws.receive_json()

    assert [eof_ack["type"], drained["type"]] == ["eof_ack", "drained"]


def test_second_context_control_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_fast_stream_timing(monkeypatch)

    with TestClient(app) as client, client.websocket_connect(STREAM_PATH) as ws:
        for _ in range(3):
            assert_valid(ws.receive_json())

        ws.send_text('{"type":"context","terms":["Proje-24"]}')
        ws.send_text('{"type":"context","terms":["İkinci"]}')
        error = ws.receive_json()
        assert_valid(error)
        assert error == {"type": "error", "msg": "invalid_client_control"}
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_eof_does_not_wait_for_stalled_draft_inference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_fast_stream_timing(
        monkeypatch,
        forced_commit_sec=60.0,
        silence_commit_sec=5.0,
    )
    draft_started = threading.Event()
    release_draft = threading.Event()

    def blocking_draft(
        self: streaming_models.DirectWhisperService,
        _audio: np.ndarray[tuple[int, ...], np.dtype[np.float32]],
        _vad: bool,
    ) -> str:
        if _is_final_service(self):
            return "Kapanış finali kalıcı."
        draft_started.set()
        release_draft.wait(timeout=2.0)
        return "Bu geç taslak yayınlanmamalı"

    monkeypatch.setattr(
        streaming_models.DirectWhisperService,
        "transcribe_array",
        blocking_draft,
    )

    with TestClient(app) as client, client.websocket_connect(STREAM_PATH) as ws:
        for _ in range(3):
            assert_valid(ws.receive_json())

        ws.send_bytes(_speech_frame())
        assert draft_started.wait(timeout=1.0)
        started = time.monotonic()
        ws.send_text('{"type":"eof"}')
        eof_ack = receive_terminal_ack(ws)
        ack_elapsed = time.monotonic() - started
        release_draft.set()
        final = ws.receive_json()
        drained = ws.receive_json()

    for event in (eof_ack, final, drained):
        assert_valid(event)
    assert ack_elapsed < 0.5
    assert [eof_ack["type"], final["type"], drained["type"]] == [
        "eof_ack",
        "final",
        "drained",
    ]


def test_eof_waits_for_published_final_state_transition_without_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_fast_stream_timing(
        monkeypatch,
        forced_commit_sec=0.1,
        silence_commit_sec=5.0,
        tail_overlap_sec=0.0,
    )
    final_reached_transport = threading.Event()
    release_final_transition = threading.Event()
    original_send_json = WebSocket.send_json

    def fake_transcribe(
        self: streaming_models.DirectWhisperService,
        _audio: np.ndarray[tuple[int, ...], np.dtype[np.float32]],
        _vad: bool,
    ) -> str:
        return "Yarışsız kalıcı final." if _is_final_service(self) else "Yarışsız taslak"

    async def blocking_final_send(
        websocket: WebSocket,
        data: Any,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        await original_send_json(websocket, data, *args, **kwargs)
        if (
            isinstance(data, dict)
            and data.get("type") == "final"
            and data.get("reason") == "forced"
            and not final_reached_transport.is_set()
        ):
            final_reached_transport.set()
            await asyncio.to_thread(release_final_transition.wait, 2.0)

    monkeypatch.setattr(
        streaming_models.DirectWhisperService,
        "transcribe_array",
        fake_transcribe,
    )
    monkeypatch.setattr(WebSocket, "send_json", blocking_final_send)

    with TestClient(app) as client, client.websocket_connect(STREAM_PATH) as ws:
        for _ in range(3):
            assert_valid(ws.receive_json())

        ws.send_bytes(_speech_frame())
        assert final_reached_transport.wait(timeout=1.0)
        pre_eof_events: list[dict[str, Any]] = []
        while True:
            event = ws.receive_json()
            pre_eof_events.append(event)
            if event["type"] == "final":
                break
        ws.send_text('{"type":"eof"}')
        time.sleep(0.05)
        release_final_transition.set()
        terminal_events = [ws.receive_json(), ws.receive_json()]

    for event in (*pre_eof_events, *terminal_events):
        assert_valid(event)
    finals = [event for event in pre_eof_events if event["type"] == "final"]
    assert len(finals) == 1
    assert finals[0]["seq"] == 0
    assert [event["type"] for event in terminal_events] == ["eof_ack", "drained"]


def test_eof_does_not_refinalize_retained_forced_commit_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_fast_stream_timing(
        monkeypatch,
        forced_commit_sec=0.1,
        silence_commit_sec=5.0,
        tail_overlap_sec=0.025,
    )
    final_calls: list[int] = []

    def fake_transcribe(
        self: streaming_models.DirectWhisperService,
        audio: np.ndarray[tuple[int, ...], np.dtype[np.float32]],
        _vad: bool,
    ) -> str:
        if _is_final_service(self):
            final_calls.append(audio.size)
            return "İlk kesin metin." if len(final_calls) == 1 else "Farklı kuyruk metni."
        return "Canlı taslak"

    monkeypatch.setattr(
        streaming_models.DirectWhisperService,
        "transcribe_array",
        fake_transcribe,
    )

    with TestClient(app) as client, client.websocket_connect(STREAM_PATH) as ws:
        for _ in range(3):
            assert_valid(ws.receive_json())

        ws.send_bytes(_speech_frame())
        pre_terminal: list[dict[str, Any]] = []
        while True:
            event = ws.receive_json()
            assert_valid(event)
            pre_terminal.append(event)
            if event["type"] == "final":
                break

        ws.send_text('{"type":"eof"}')
        terminal = [ws.receive_json(), ws.receive_json()]

    assert [event["type"] for event in terminal] == ["eof_ack", "drained"]
    assert len([event for event in pre_terminal if event["type"] == "final"]) == 1
    assert final_calls == [1024]


def test_blocked_final_transport_closes_bounded_without_drained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_fast_stream_timing(
        monkeypatch,
        forced_commit_sec=0.1,
        silence_commit_sec=5.0,
        tail_overlap_sec=0.0,
        transport_timeout_sec=0.05,
    )
    final_reached_transport = threading.Event()
    original_send_json = WebSocket.send_json

    def fake_transcribe(
        self: streaming_models.DirectWhisperService,
        _audio: np.ndarray[tuple[int, ...], np.dtype[np.float32]],
        _vad: bool,
    ) -> str:
        return "Sınırlı final." if _is_final_service(self) else "Sınırlı taslak"

    async def never_returning_final_send(
        websocket: WebSocket,
        data: Any,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        await original_send_json(websocket, data, *args, **kwargs)
        if (
            isinstance(data, dict)
            and data.get("type") == "final"
            and data.get("reason") == "forced"
        ):
            final_reached_transport.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(
        streaming_models.DirectWhisperService,
        "transcribe_array",
        fake_transcribe,
    )
    monkeypatch.setattr(WebSocket, "send_json", never_returning_final_send)

    with TestClient(app) as client, client.websocket_connect(STREAM_PATH) as ws:
        for _ in range(3):
            assert_valid(ws.receive_json())

        ws.send_bytes(_speech_frame())
        assert final_reached_transport.wait(timeout=1.0)
        started = time.monotonic()
        ws.send_text('{"type":"eof"}')
        observed_types: list[str] = []
        with pytest.raises(WebSocketDisconnect):
            while True:
                observed_types.append(str(ws.receive_json()["type"]))
        elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert "final" in observed_types
    assert "drained" not in observed_types


def test_background_final_transport_timeout_closes_before_accepting_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_fast_stream_timing(
        monkeypatch,
        forced_commit_sec=0.1,
        silence_commit_sec=5.0,
        tail_overlap_sec=0.0,
        transport_timeout_sec=0.05,
    )
    final_reached_transport = threading.Event()
    original_send_json = WebSocket.send_json

    def fake_transcribe(
        self: streaming_models.DirectWhisperService,
        _audio: np.ndarray[tuple[int, ...], np.dtype[np.float32]],
        _vad: bool,
    ) -> str:
        return "Tek final." if _is_final_service(self) else "Tek taslak"

    async def delivered_then_blocked_final(
        websocket: WebSocket,
        data: Any,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        await original_send_json(websocket, data, *args, **kwargs)
        if isinstance(data, dict) and data.get("type") == "final":
            final_reached_transport.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(
        streaming_models.DirectWhisperService,
        "transcribe_array",
        fake_transcribe,
    )
    monkeypatch.setattr(WebSocket, "send_json", delivered_then_blocked_final)

    with TestClient(app) as client, client.websocket_connect(STREAM_PATH) as ws:
        for _ in range(3):
            assert_valid(ws.receive_json())

        ws.send_bytes(_speech_frame())
        assert final_reached_transport.wait(timeout=1.0)
        observed: list[dict[str, Any]] = []
        with pytest.raises(WebSocketDisconnect):
            while True:
                observed.append(ws.receive_json())

    finals = [event for event in observed if event.get("type") == "final"]
    assert len(finals) == 1
    assert (
        len(
            {
                (
                    event["seq"],
                    event["source_start_sample"],
                    event["source_end_sample"],
                )
                for event in finals
            }
        )
        == 1
    )
    assert all(event.get("type") != "drained" for event in observed)


def test_terminal_deadline_does_not_wait_for_cancellation_resistant_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_fast_stream_timing(
        monkeypatch,
        forced_commit_sec=60.0,
        silence_commit_sec=5.0,
        final_timeout_sec=1.0,
        kill_grace_sec=0.0,
        transport_timeout_sec=0.05,
    )
    final_started = threading.Event()
    release_final = threading.Event()

    def blocking_final(
        self: streaming_models.DirectWhisperService,
        _audio: np.ndarray[tuple[int, ...], np.dtype[np.float32]],
        _vad: bool,
    ) -> str:
        if not _is_final_service(self):
            return "Bekleyen taslak"
        final_started.set()
        release_final.wait(timeout=3.0)
        return "Bütçe sonrası yayınlanmamalı."

    monkeypatch.setattr(
        streaming_models.DirectWhisperService,
        "transcribe_array",
        blocking_final,
    )

    try:
        with TestClient(app) as client, client.websocket_connect(STREAM_PATH) as ws:
            for _ in range(3):
                assert_valid(ws.receive_json())

            ws.send_bytes(_speech_frame())
            started = time.monotonic()
            ws.send_text('{"type":"eof"}')
            assert receive_terminal_ack(ws) == {"type": "eof_ack"}
            assert final_started.wait(timeout=1.0)
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
            elapsed = time.monotonic() - started
    finally:
        release_final.set()

    # 1.0s model + 6*0.05s transport is the advertised absolute budget.
    assert elapsed < 1.7


def test_eof_flushes_late_final_before_drained(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_fast_stream_timing(
        monkeypatch,
        forced_commit_sec=60.0,
        silence_commit_sec=5.0,
    )

    def fake_transcribe(
        self: streaming_models.DirectWhisperService,
        _audio: np.ndarray[tuple[int, ...], np.dtype[np.float32]],
        _vad: bool,
    ) -> str:
        return "Son kelimeler kalıcı final." if _is_final_service(self) else "Son kelimeler"

    monkeypatch.setattr(streaming_models.DirectWhisperService, "transcribe_array", fake_transcribe)

    with TestClient(app) as client, client.websocket_connect(STREAM_PATH) as ws:
        for _ in range(3):
            assert_valid(ws.receive_json())

        ws.send_bytes(_speech_frame())
        ws.send_text('{"type":"eof"}')
        eof_ack = receive_terminal_ack(ws)
        events = [eof_ack, ws.receive_json(), ws.receive_json()]

    for event in events:
        assert_valid(event)
    assert [event["type"] for event in events] == ["eof_ack", "final", "drained"]
    assert events[1]["reason"] == "eof"
    assert events[1]["text"] == "Son kelimeler kalıcı final."


def test_unknown_text_control_is_rejected_without_drained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_fast_stream_timing(monkeypatch)

    with TestClient(app) as client, client.websocket_connect(STREAM_PATH) as ws:
        for _ in range(3):
            assert_valid(ws.receive_json())

        ws.send_text('{"type":"ping"}')
        error = ws.receive_json()
        assert_valid(error)
        assert error == {"type": "error", "msg": "invalid_client_control"}
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


@pytest.mark.parametrize("late_frame", ["eof", "binary"])
def test_second_or_post_eof_frame_is_rejected_without_drained(
    monkeypatch: pytest.MonkeyPatch,
    late_frame: str,
) -> None:
    _patch_fast_stream_timing(
        monkeypatch,
        forced_commit_sec=60.0,
        silence_commit_sec=5.0,
    )
    final_started = threading.Event()
    release_final = threading.Event()

    def blocking_transcribe(
        self: streaming_models.DirectWhisperService,
        _audio: np.ndarray[tuple[int, ...], np.dtype[np.float32]],
        _vad: bool,
    ) -> str:
        if not _is_final_service(self):
            return "Bekleyen taslak"
        final_started.set()
        release_final.wait(timeout=2.0)
        return "Bu final yayınlanmamalı."

    monkeypatch.setattr(
        streaming_models.DirectWhisperService,
        "transcribe_array",
        blocking_transcribe,
    )

    with TestClient(app) as client, client.websocket_connect(STREAM_PATH) as ws:
        for _ in range(3):
            assert_valid(ws.receive_json())

        ws.send_bytes(_speech_frame())
        ws.send_text('{"type":"eof"}')
        eof_ack = receive_terminal_ack(ws)
        assert eof_ack == {"type": "eof_ack"}
        assert final_started.wait(timeout=1.0)
        if late_frame == "eof":
            ws.send_text('{"type":"eof"}')
        else:
            ws.send_bytes(_speech_frame())
        release_final.set()

        error = ws.receive_json()
        assert_valid(error)
        assert error == {"type": "error", "msg": "post_eof_frame"}
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_terminal_final_model_error_never_emits_drained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_fast_stream_timing(
        monkeypatch,
        forced_commit_sec=60.0,
        silence_commit_sec=5.0,
    )

    def failing_final(
        self: streaming_models.DirectWhisperService,
        _audio: np.ndarray[tuple[int, ...], np.dtype[np.float32]],
        _vad: bool,
    ) -> str:
        if _is_final_service(self):
            raise RuntimeError("terminal final failed")
        return "Taslak"

    monkeypatch.setattr(streaming_models.DirectWhisperService, "transcribe_array", failing_final)

    with TestClient(app) as client, client.websocket_connect(STREAM_PATH) as ws:
        for _ in range(3):
            assert_valid(ws.receive_json())

        ws.send_bytes(_speech_frame())
        ws.send_text('{"type":"eof"}')
        assert receive_terminal_ack(ws) == {"type": "eof_ack"}
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_contract_rejects_unknown_event_type() -> None:
    errors = list(VALIDATOR.iter_errors({"type": "telemetry", "x": 1}))
    assert errors, "schema must reject unknown event types (drift gate)"


def _speech_frame() -> bytes:
    return (np.ones(1024, dtype=np.float32) * 0.05).tobytes()


def _speech_frame_with_level(level: float) -> bytes:
    return (np.ones(1024, dtype=np.float32) * level).tobytes()


def _silence_frame() -> bytes:
    return np.zeros(1024, dtype=np.float32).tobytes()


def _fixture_audio(name: str) -> np.ndarray[tuple[int, ...], np.dtype[np.float32]]:
    spec = SILERO_FIXTURES[name]
    target_rms = float(spec["target_rms"])
    if spec.get("kind") == "sine":
        sample_count = int(16_000 * float(spec["duration_sec"]))
        timeline = np.arange(sample_count, dtype=np.float32) / 16_000
        audio = np.sin(2 * np.pi * float(spec["frequency_hz"]) * timeline)
    else:
        with wave.open(str(FIXTURE_DIR / str(spec["source"])), "rb") as fixture:
            assert fixture.getnchannels() == 1
            assert fixture.getsampwidth() == 2
            source_rate = fixture.getframerate()
            audio = (
                np.frombuffer(
                    fixture.readframes(fixture.getnframes()),
                    dtype="<i2",
                ).astype(np.float32)
                / 32768.0
            )
        assert source_rate == 48_000
        audio = audio[::3]
        start = int(float(spec["start_sec"]) * 16_000)
        end = start + int(float(spec["duration_sec"]) * 16_000)
        audio = audio[start:end]

    rms = float(np.sqrt(np.mean(np.square(audio))))
    assert rms > 0
    return np.asarray(audio * (target_rms / rms), dtype=np.float32)


def _audio_frames(audio: np.ndarray[tuple[int, ...], np.dtype[np.float32]]) -> Iterator[bytes]:
    for offset in range(0, audio.size, 1024):
        yield audio[offset : offset + 1024].tobytes()


class _SileroBackedDecoderStub:
    """Run the real pinned VAD and stub only the expensive Whisper decode."""

    def __init__(self, role: str, calls: list[dict[str, object]]) -> None:
        self.role = role
        self.calls = calls

    def transcribe(
        self,
        audio: np.ndarray[tuple[int, ...], np.dtype[np.float32]],
        **kwargs: object,
    ) -> tuple[list[object], object]:
        assert kwargs["vad_filter"] is True
        raw_parameters = kwargs["vad_parameters"]
        assert isinstance(raw_parameters, dict)
        options = VadOptions(**raw_parameters)
        timestamps = get_speech_timestamps(audio, options)
        decoder_audio = collect_chunks(audio, timestamps)
        self.calls.append(
            {
                "role": self.role,
                "parameters": raw_parameters,
                "input_samples": audio.size,
                "decoder_samples": decoder_audio.size,
            }
        )
        if decoder_audio.size == 0:
            return [], object()
        return [SimpleNamespace(text="Sessiz masaustu konusmasi")], object()


def _install_silero_decoder_stub(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[dict[str, object]],
) -> None:
    def install(self: streaming_models.DirectWhisperService) -> None:
        self._model = _SileroBackedDecoderStub(self.role, calls)

    monkeypatch.setattr(streaming_models.DirectWhisperService, "ensure_model", install)


def _patch_fast_stream_timing(
    monkeypatch: pytest.MonkeyPatch,
    *,
    forced_commit_sec: float = 60.0,
    silence_commit_sec: float = 0.1,
    tail_overlap_sec: float = 0.0,
    final_timeout_sec: float = 30.0,
    kill_grace_sec: float = 2.0,
    transport_timeout_sec: float = 2.0,
) -> None:
    settings = Settings(
        live_infer_interval_ms=1,
        live_window_sec=1.0,
        final_window_sec=5.0,
        forced_commit_sec=forced_commit_sec,
        silence_commit_sec=silence_commit_sec,
        tail_overlap_sec=tail_overlap_sec,
        stream_final_timeout_sec=final_timeout_sec,
        worker_kill_grace_sec=kill_grace_sec,
        stream_transport_timeout_sec=transport_timeout_sec,
        silence_rms=0.001,
        min_speech_rms=0.001,
        min_infer_sec=0.01,
    )
    app.dependency_overrides[get_settings] = lambda: settings
    monkeypatch.setattr(streaming_models.DirectWhisperService, "ensure_model", lambda self: None)


def _is_final_service(service: streaming_models.DirectWhisperService) -> bool:
    return getattr(service, "role", "") == "final"


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
        return "Merhaba nasılsın." if _is_final_service(self) else next(live_drafts)

    monkeypatch.setattr(streaming_models.DirectWhisperService, "transcribe_array", fake_transcribe)

    with TestClient(app) as client, client.websocket_connect(STREAM_PATH) as ws:
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
    assert first["confirmed"] == ""
    assert first["tentative"] == "Merhaba"
    assert second["confirmed"] == "Merhaba"
    assert second["tentative"] == "nasılsın"


def test_stream_default_gate_accepts_quiet_desktop_microphone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Desktop/WebAudio speech around RMS 0.002 must not be treated as silence."""

    settings = Settings(
        live_infer_interval_ms=1,
        final_window_sec=5.0,
        forced_commit_sec=60.0,
        silence_commit_sec=0.1,
        speech_gate_profile="silero-balanced-v1",
        stream_live_vad_filter=True,
        stream_final_vad_filter=True,
    )
    app.dependency_overrides[get_settings] = lambda: settings
    monkeypatch.setattr(streaming_models.DirectWhisperService, "ensure_model", lambda self: None)

    live_vad_flags: list[bool] = []

    def fake_transcribe(
        self: streaming_models.DirectWhisperService,
        _audio: np.ndarray[tuple[int, ...], np.dtype[np.float32]],
        vad: bool,
    ) -> str:
        if not _is_final_service(self):
            live_vad_flags.append(vad)
        return (
            "Sessiz olmayan konuşma."
            if _is_final_service(self)
            else "Sessiz olmayan masaüstü konuşması"
        )

    monkeypatch.setattr(streaming_models.DirectWhisperService, "transcribe_array", fake_transcribe)

    with TestClient(app) as client, client.websocket_connect(STREAM_PATH) as ws:
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
    assert live_vad_flags == [True]


def test_stream_production_gate_suppresses_above_floor_pause_noise_with_vad(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real pinned Silero rejects above-floor non-speech in both WS roles."""
    settings = Settings(
        live_infer_interval_ms=1,
        live_window_sec=2.0,
        final_window_sec=6.0,
        forced_commit_sec=60.0,
        silence_commit_sec=5.0,
        min_infer_sec=0.01,
        speech_gate_profile="silero-balanced-v1",
        stream_live_vad_filter=True,
        stream_final_vad_filter=True,
    )
    app.dependency_overrides[get_settings] = lambda: settings
    decode_calls: list[dict[str, object]] = []
    _install_silero_decoder_stub(monkeypatch, decode_calls)

    with TestClient(app) as client, client.websocket_connect(STREAM_PATH) as ws:
        for _ in range(3):
            assert_valid(ws.receive_json())
        for frame in _audio_frames(_fixture_audio("above_floor_non_speech")):
            ws.send_bytes(frame)
            time.sleep(0.002)
        time.sleep(0.05)
        ws.send_text('{"type":"eof"}')
        eof_ack = receive_terminal_ack(ws)
        drained = ws.receive_json()

    assert [eof_ack["type"], drained["type"]] == ["eof_ack", "drained"]
    expected_vad = {
        "threshold": 0.35,
        "min_speech_duration_ms": 100,
        "min_silence_duration_ms": 300,
        "speech_pad_ms": 100,
    }
    assert {call["role"] for call in decode_calls} == {"live", "final"}
    assert all(call["parameters"] == expected_vad for call in decode_calls)
    assert all(
        isinstance(call["input_samples"], int) and call["input_samples"] > 0
        for call in decode_calls
    )
    assert all(call["decoder_samples"] == 0 for call in decode_calls)


def test_stream_production_gate_keeps_quiet_speech_with_pinned_final_vad(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real pinned Silero passes quiet speech to both WS decoder roles."""
    settings = Settings(
        live_infer_interval_ms=1,
        live_window_sec=2.0,
        final_window_sec=6.0,
        forced_commit_sec=60.0,
        silence_commit_sec=5.0,
        silence_rms=0.0005,
        min_speech_rms=0.0005,
        min_infer_sec=0.01,
        speech_gate_profile="silero-balanced-v1",
        stream_live_vad_filter=True,
        stream_final_vad_filter=True,
    )
    app.dependency_overrides[get_settings] = lambda: settings
    decode_calls: list[dict[str, object]] = []
    _install_silero_decoder_stub(monkeypatch, decode_calls)

    with TestClient(app) as client, client.websocket_connect(STREAM_PATH) as ws:
        for _ in range(3):
            assert_valid(ws.receive_json())

        for frame in _audio_frames(_fixture_audio("quiet_speech")):
            ws.send_bytes(frame)
            time.sleep(0.002)
        partial = ws.receive_json()
        ws.send_text('{"type":"eof"}')
        eof_ack = receive_terminal_ack(ws)
        final = ws.receive_json()
        drained = ws.receive_json()

    assert_valid(partial)
    assert partial["type"] == "partial"
    assert partial["tentative"] == "Sessiz masaustu konusmasi"
    assert eof_ack["type"] == "eof_ack"
    assert_valid(final)
    assert final["type"] == "final"
    assert final["text"] == "Sessiz masaustu konusmasi"
    assert drained["type"] == "drained"
    assert {call["role"] for call in decode_calls} == {"live", "final"}
    assert all(
        isinstance(call["decoder_samples"], int) and call["decoder_samples"] > 0
        for call in decode_calls
    )


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
        if _is_final_service(self):
            final_sample_counts.append(int(audio.size))
            return "Canlı final metin."
        time.sleep(0.08)
        return "Canlı taslak"

    monkeypatch.setattr(streaming_models.DirectWhisperService, "transcribe_array", fake_transcribe)

    with TestClient(app) as client, client.websocket_connect(STREAM_PATH) as ws:
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
        tail_overlap_sec=0.01,
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
        if _is_final_service(self):
            final_sample_counts.append(int(audio.size))
            if len(final_sample_counts) == 1:
                time.sleep(0.12)
            return f"Final {len(final_sample_counts)}."
        return ""

    monkeypatch.setattr(streaming_models.DirectWhisperService, "transcribe_array", fake_transcribe)

    with TestClient(app) as client, client.websocket_connect(STREAM_PATH) as ws:
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
    assert (
        first_final["source_end_sample"] - first_final["source_start_sample"]
        == final_sample_counts[0]
    )
    assert (
        second_final["source_end_sample"] - second_final["source_start_sample"]
        == final_sample_counts[1]
    )
    assert second_final["source_start_sample"] < first_final["source_end_sample"]
    assert second_final["source_end_sample"] > first_final["source_end_sample"]


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
        return (
            "Merhaba sesim geliyor mu bir sürü eksik var yine."
            if _is_final_service(self)
            else next(live_drafts)
        )

    monkeypatch.setattr(streaming_models.DirectWhisperService, "transcribe_array", fake_transcribe)

    with TestClient(app) as client, client.websocket_connect(STREAM_PATH) as ws:
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
    assert first["confirmed"] == ""
    assert first["tentative"] == "Merhaba sesim geliyor mu"
    assert second["confirmed"] == "Merhaba sesim geliyor mu"
    assert second["tentative"] == "bir sürü eksik var yine"


def test_stream_appends_short_no_overlap_live_continuations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Short new live windows after a stable draft must not be filtered out."""
    _patch_fast_stream_timing(monkeypatch)
    live_drafts = [
        "Konuşulanların çok büyük kısmı yazılmıyor",
        "ara kelimeler düşüyor",
    ]

    def fake_transcribe(
        self: streaming_models.DirectWhisperService,
        _audio: np.ndarray[tuple[int, ...], np.dtype[np.float32]],
        vad: bool,
    ) -> str:
        if _is_final_service(self):
            return "Konuşulanların çok büyük kısmı yazılmıyor ara kelimeler düşüyor."
        return live_drafts.pop(0) if live_drafts else "ara kelimeler düşüyor"

    monkeypatch.setattr(streaming_models.DirectWhisperService, "transcribe_array", fake_transcribe)

    with TestClient(app) as client, client.websocket_connect(STREAM_PATH) as ws:
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
    assert first["confirmed"] == ""
    assert first["tentative"] == "Konuşulanların çok büyük kısmı yazılmıyor"
    assert second["confirmed"] == "Konuşulanların çok büyük kısmı yazılmıyor"
    assert second["tentative"] == "ara kelimeler düşüyor"


def test_stream_revises_competing_same_opener_tail_without_fabricating_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Competing same-opener windows revise only the tentative tail."""
    _patch_fast_stream_timing(monkeypatch)
    live_drafts = [
        "Merhaba burada hava çok",
        "Merhaba atıyorsun çok değişik şeyler",
    ]

    def fake_transcribe(
        self: streaming_models.DirectWhisperService,
        _audio: np.ndarray[tuple[int, ...], np.dtype[np.float32]],
        vad: bool,
    ) -> str:
        if _is_final_service(self):
            return "Merhaba burada hava çok atıyorsun çok değişik şeyler."
        return live_drafts.pop(0) if live_drafts else "Merhaba atıyorsun çok değişik şeyler"

    monkeypatch.setattr(streaming_models.DirectWhisperService, "transcribe_array", fake_transcribe)

    with TestClient(app) as client, client.websocket_connect(STREAM_PATH) as ws:
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
    assert first["confirmed"] == ""
    assert first["tentative"] == "Merhaba burada hava çok"
    assert second["confirmed"] == "Merhaba"
    assert second["tentative"] == "atıyorsun çok değişik şeyler"


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
        return "Neroba" if _is_final_service(self) else "Merhaba"

    monkeypatch.setattr(streaming_models.DirectWhisperService, "transcribe_array", fake_transcribe)

    with TestClient(app) as client, client.websocket_connect(STREAM_PATH) as ws:
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


def test_stream_keeps_medium_draft_over_short_unrelated_final(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finalization must not erase already displayed medium-length speech."""
    _patch_fast_stream_timing(monkeypatch)
    draft_text = "Konuşulanların çok büyük kısmı yazılmıyor ara kelimeler düşüyor"

    def fake_transcribe(
        self: streaming_models.DirectWhisperService,
        _audio: np.ndarray[tuple[int, ...], np.dtype[np.float32]],
        vad: bool,
    ) -> str:
        return "Görüşmek üzere canı çıkmak için" if _is_final_service(self) else draft_text

    monkeypatch.setattr(streaming_models.DirectWhisperService, "transcribe_array", fake_transcribe)

    with TestClient(app) as client, client.websocket_connect(STREAM_PATH) as ws:
        for _ in range(3):
            assert_valid(ws.receive_json())

        ws.send_bytes(_speech_frame())
        partial = ws.receive_json()
        time.sleep(0.11)
        ws.send_bytes(_silence_frame())
        final = ws.receive_json()

    assert_valid(partial)
    assert partial["type"] == "partial"
    assert partial["tentative"] == draft_text

    assert_valid(final)
    assert final["type"] == "final"
    assert final["text"] == draft_text


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
        return "Merhaba nasılsın." if _is_final_service(self) else "Merhaba nasılsın"

    monkeypatch.setattr(streaming_models.DirectWhisperService, "transcribe_array", fake_transcribe)

    with TestClient(app) as client, client.websocket_connect(STREAM_PATH) as ws:
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
        return "Uzun konuşma final." if _is_final_service(self) else "Uzun konuşma"

    monkeypatch.setattr(streaming_models.DirectWhisperService, "transcribe_array", fake_transcribe)

    with TestClient(app) as client, client.websocket_connect(STREAM_PATH) as ws:
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
        if _is_final_service(self):
            final_first_samples.append(float(audio[0]))
            return f"Final {len(final_first_samples)}."
        return "Taslak"

    monkeypatch.setattr(streaming_models.DirectWhisperService, "transcribe_array", fake_transcribe)

    with TestClient(app) as client, client.websocket_connect(STREAM_PATH) as ws:
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
