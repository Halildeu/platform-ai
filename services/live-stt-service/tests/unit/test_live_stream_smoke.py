"""Tests for the transcript-free live stream smoke client."""

# ruff: noqa: RUF001 - Turkish fixture text is intentional.

from __future__ import annotations

import asyncio
import gc
import importlib.util
import json
import time
from pathlib import Path
from types import ModuleType

import numpy as np
import pytest
from websockets.frames import Close

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = ROOT / "scripts" / "live_stream_smoke.py"
FIXTURE = ROOT / "tests" / "fixtures" / "sample-tr-cv17-001.wav"


def _load_smoke_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("live_stream_smoke", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_default_url_negotiates_source_range_protocol() -> None:
    smoke = _load_smoke_module()

    args = smoke.parse_args([])

    assert args.url.endswith("/ws/stream?protocol=source-ranges-v1")
    assert args.final_wait_sec == 90.0


def test_ready_contract_requires_exact_protocol_and_terminal_budget() -> None:
    smoke = _load_smoke_module()
    ready = {
        "type": "ready",
        "sample_rate": 16_000,
        "live_model": "fixture-live",
        "final_model": "fixture-final",
        "partial_mode": "stable-v1",
        "protocol": "source-ranges-v1",
        "capabilities": ["eof", "source-ranges-v1"],
        "supports_eof": True,
        "terminal_timeout_ms": 60_000,
    }

    smoke.validate_ready_event(ready)

    for key, invalid in (
        ("protocol", "legacy"),
        ("capabilities", ["eof"]),
        ("supports_eof", False),
        ("terminal_timeout_ms", 0),
    ):
        incompatible = {**ready, key: invalid}
        with pytest.raises(smoke.SmokeError, match="ready event"):
            smoke.validate_ready_event(incompatible)


@pytest.mark.parametrize(
    ("confirmed", "tentative"),
    [
        ("", ""),
        ("   ", "  "),
        ("...", "!!!"),
        ("Altyazı", "M.K."),
    ],
)
def test_partial_contract_rejects_empty_or_junk_content(
    confirmed: str, tentative: str
) -> None:
    smoke = _load_smoke_module()
    event = {
        "type": "partial",
        "seq": 0,
        "confirmed": confirmed,
        "tentative": tentative,
        "elapsed_ms": 100,
        "rms": 0.02,
        "source": "fixture-live",
    }

    with pytest.raises(smoke.SmokeError, match="partial event"):
        smoke.validate_transcript_event(
            event,
            cumulative_samples_sent=16_000,
            previous_final_seq=None,
        )


def test_run_smoke_validates_real_fake_websocket_handshake(monkeypatch: pytest.MonkeyPatch) -> None:
    smoke = _load_smoke_module()
    events = [
        {"type": "loading", "stage": "live_model"},
        {"type": "loading", "stage": "final_model"},
        {
            "type": "ready",
            "sample_rate": 16_000,
            "live_model": "fixture-live",
            "final_model": "fixture-final",
            "partial_mode": "stable-v1",
            "protocol": "source-ranges-v1",
            "capabilities": ["eof", "source-ranges-v1"],
            "supports_eof": True,
            "terminal_timeout_ms": 60_000,
        },
        {
            "type": "partial",
            "seq": 0,
            "confirmed": "Geçiş ülkelerinde",
            "tentative": "yaşananlar",
            "elapsed_ms": 100,
            "rms": 0.02,
            "source": "fixture-live",
        },
        {
            "type": "final",
            "seq": 0,
            "text": "Geçiş ülkelerinde yaşananlar ise karışık.",
            "reason": "eof",
            "elapsed_ms": 200,
            "rms": 0.02,
            "source_start_sample": 0,
            "source_end_sample": 16_000,
        },
    ]

    class FakeWebsocket:
        def __init__(self) -> None:
            self.sent: list[bytes | str] = []
            self.terminal_ready = asyncio.Event()
            self.closed = False

        async def recv(self) -> str:
            while not events:
                if self.closed:
                    close = Close(1000, "")
                    raise smoke.ConnectionClosedOK(close, close, True)
                await self.terminal_ready.wait()
            return json.dumps(events.pop(0))

        async def send(self, payload: bytes | str) -> None:
            self.sent.append(payload)
            if payload == '{"type":"eof"}':
                events.extend([{"type": "eof_ack"}, {"type": "drained"}])
                self.closed = True
                self.terminal_ready.set()

    websocket = FakeWebsocket()

    class FakeConnection:
        async def __aenter__(self) -> FakeWebsocket:
            return websocket

        async def __aexit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(smoke.websockets, "connect", lambda *_args, **_kwargs: FakeConnection())
    monkeypatch.setattr(
        smoke,
        "audio_frames",
        lambda audio, **_kwargs: (
            [np.asarray(audio[:16_000], dtype=np.float32)] if audio.size else []
        ),
    )
    args = smoke.parse_args(
        [
            "--wav",
            str(FIXTURE),
            "--tail-silence-sec",
            "0",
            "--final-wait-sec",
            "1",
        ]
    )

    summary = asyncio.run(smoke.run_smoke(args))

    assert summary["ok"] is True
    assert summary["events"]["terminal_sequence"] == ["eof_ack", "drained"]
    assert len(websocket.sent) == 2
    assert isinstance(websocket.sent[0], bytes)
    assert websocket.sent[1] == '{"type":"eof"}'
    assert summary["fixture"]["streamed_samples"] == 16_000


def test_transcript_contract_rejects_final_without_source_range() -> None:
    smoke = _load_smoke_module()

    with pytest.raises(smoke.SmokeError, match="final event violates"):
        smoke.validate_transcript_event(
            {
                "type": "final",
                "seq": 0,
                "text": "Eksik final",
                "reason": "eof",
                "elapsed_ms": 200,
                "rms": 0.02,
            },
            cumulative_samples_sent=16_000,
            previous_final_seq=None,
        )


def test_transcript_contract_rejects_future_range_and_non_increasing_final_seq() -> None:
    smoke = _load_smoke_module()
    final = {
        "type": "final",
        "seq": 0,
        "text": "Kaynağa bağlı final",
        "reason": "eof",
        "elapsed_ms": 200,
        "rms": 0.02,
        "source_start_sample": 0,
        "source_end_sample": 16_000,
    }

    smoke.validate_transcript_event(
        final,
        cumulative_samples_sent=16_000,
        previous_final_seq=None,
    )
    with pytest.raises(smoke.SmokeError, match="final event violates"):
        smoke.validate_transcript_event(
            {**final, "source_end_sample": 16_001},
            cumulative_samples_sent=16_000,
            previous_final_seq=None,
        )
    with pytest.raises(smoke.SmokeError, match="final event violates"):
        smoke.validate_transcript_event(
            final,
            cumulative_samples_sent=16_000,
            previous_final_seq=0,
        )
    smoke.validate_transcript_event(
        {**final, "seq": 7},
        cumulative_samples_sent=16_000,
        previous_final_seq=None,
    )
    smoke.validate_transcript_event(
        {**final, "seq": 9},
        cumulative_samples_sent=16_000,
        previous_final_seq=7,
        previous_final_source_end=15_999,
    )
    with pytest.raises(smoke.SmokeError, match="final event violates"):
        smoke.validate_transcript_event(
            {**final, "seq": 10},
            cumulative_samples_sent=16_000,
            previous_final_seq=9,
            previous_final_source_end=16_000,
        )
    with pytest.raises(smoke.SmokeError, match="final event violates"):
        smoke.validate_transcript_event(
            {**final, "seq": 6},
            cumulative_samples_sent=16_000,
            previous_final_seq=7,
        )


def test_run_smoke_rejects_terminal_ack_before_local_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke = _load_smoke_module()
    events = [
        {"type": "loading", "stage": "live_model"},
        {"type": "loading", "stage": "final_model"},
        {
            "type": "ready",
            "sample_rate": 16_000,
            "live_model": "fixture-live",
            "final_model": "fixture-final",
            "partial_mode": "stable-v1",
            "protocol": "source-ranges-v1",
            "capabilities": ["eof", "source-ranges-v1"],
            "supports_eof": True,
            "terminal_timeout_ms": 60_000,
        },
        {"type": "eof_ack"},
    ]

    class FakeWebsocket:
        async def recv(self) -> str:
            return json.dumps(events.pop(0))

        async def send(self, _payload: bytes | str) -> None:
            return None

    websocket = FakeWebsocket()

    class FakeConnection:
        async def __aenter__(self) -> FakeWebsocket:
            return websocket

        async def __aexit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(smoke.websockets, "connect", lambda *_args, **_kwargs: FakeConnection())
    monkeypatch.setattr(
        smoke,
        "audio_frames",
        lambda audio, **_kwargs: [np.asarray(audio[:1], dtype=np.float32)],
    )
    args = smoke.parse_args(["--wav", str(FIXTURE), "--tail-silence-sec", "0", "--frame-ms", "1"])

    with pytest.raises(smoke.SmokeError, match="not caused by local eof"):
        asyncio.run(smoke.run_smoke(args))


@pytest.mark.parametrize(
    ("terminal_events", "expected_error"),
    [
        (
            [
                {"type": "eof_ack"},
                {
                    "type": "partial",
                    "seq": 0,
                    "confirmed": "",
                    "tentative": "gecikmis",
                    "elapsed_ms": 1,
                    "rms": 0.01,
                    "source": "fixture-live",
                },
            ],
            "partial event received after eof_ack",
        ),
        (
            [
                {"type": "eof_ack"},
                {"type": "drained"},
                {"type": "error", "msg": "trailing"},
            ],
            "trailing event received after drained",
        ),
        (
            [
                {"type": "eof_ack"},
                {"type": "ready"},
                {"type": "drained"},
            ],
            "unexpected event type in stream state machine",
        ),
        (
            [
                {"type": "eof_ack"},
                {"type": "telemetry"},
                {"type": "drained"},
            ],
            "unexpected event type in stream state machine",
        ),
    ],
)
def test_run_smoke_rejects_invalid_post_ack_sequence(
    monkeypatch: pytest.MonkeyPatch,
    terminal_events: list[dict[str, object]],
    expected_error: str,
) -> None:
    smoke = _load_smoke_module()
    events: list[dict[str, object]] = [
        {"type": "loading", "stage": "live_model"},
        {"type": "loading", "stage": "final_model"},
        {
            "type": "ready",
            "sample_rate": 16_000,
            "live_model": "fixture-live",
            "final_model": "fixture-final",
            "partial_mode": "stable-v1",
            "protocol": "source-ranges-v1",
            "capabilities": ["eof", "source-ranges-v1"],
            "supports_eof": True,
            "terminal_timeout_ms": 60_000,
        },
    ]
    terminal_ready = asyncio.Event()

    class FakeWebsocket:
        async def recv(self) -> str:
            while not events:
                await terminal_ready.wait()
            return json.dumps(events.pop(0))

        async def send(self, payload: bytes | str) -> None:
            if payload == '{"type":"eof"}':
                events.extend(terminal_events)
                terminal_ready.set()

    websocket = FakeWebsocket()

    class FakeConnection:
        async def __aenter__(self) -> FakeWebsocket:
            return websocket

        async def __aexit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(smoke.websockets, "connect", lambda *_args, **_kwargs: FakeConnection())
    monkeypatch.setattr(
        smoke,
        "audio_frames",
        lambda audio, **_kwargs: [np.asarray(audio[:1], dtype=np.float32)],
    )
    args = smoke.parse_args(["--wav", str(FIXTURE), "--tail-silence-sec", "0", "--frame-ms", "1"])

    with pytest.raises(smoke.SmokeError, match=expected_error):
        asyncio.run(smoke.run_smoke(args))


def test_redacted_final_retains_numeric_source_range_without_text() -> None:
    smoke = _load_smoke_module()
    raw_text = "Bu metin kanıta girmemeli"

    redacted = smoke.redacted_transcript_event(
        {
            "type": "final",
            "seq": 3,
            "text": raw_text,
            "reason": "eof",
            "elapsed_ms": 200,
            "rms": 0.02,
            "source_start_sample": 32_000,
            "source_end_sample": 48_000,
        },
        900,
    )

    assert redacted["source_start_sample"] == 32_000
    assert redacted["source_end_sample"] == 48_000
    assert raw_text not in json.dumps(redacted, ensure_ascii=False)


def test_wav_loader_resamples_common_voice_fixture_to_16khz_float32() -> None:
    smoke = _load_smoke_module()

    audio = smoke.load_wav_float32(FIXTURE)

    assert audio.dtype.name == "float32"
    assert 88_000 <= audio.shape[0] <= 89_000
    assert float(audio.max()) <= 1.0
    assert float(audio.min()) >= -1.0


def test_redacted_summary_excludes_transcript_text() -> None:
    smoke = _load_smoke_module()
    raw_text = FIXTURE.with_suffix(".txt").read_text(encoding="utf-8").strip()
    started_at = time.perf_counter()

    summary = smoke.build_summary(
        url="ws://127.0.0.1:18220/ws/stream",
        wav_path=FIXTURE,
        audio_samples=88_320,
        started_at=started_at,
        loading_events=["loading:live_model", "loading:final_model"],
        ready_at=started_at + 0.1,
        transcript_events=[
            smoke.redacted_transcript_event(
                {
                    "type": "partial",
                    "seq": 0,
                    "confirmed": "",
                    "tentative": "Kelime akışı",
                    "elapsed_ms": 400,
                    "rms": 0.03,
                },
                500,
            ),
            smoke.redacted_transcript_event(
                {
                    "type": "final",
                    "seq": 0,
                    "text": raw_text,
                    "elapsed_ms": 700,
                    "rms": 0.03,
                },
                1500,
            ),
        ],
        terminal_events=["eof_ack", "drained"],
        errors=[],
        reference_text_path=FIXTURE.with_suffix(".txt"),
        final_transcript_text=raw_text,
    )
    payload = json.dumps(summary, ensure_ascii=False)
    raw_reference = FIXTURE.with_suffix(".txt").read_text(encoding="utf-8").strip()

    assert summary["ok"] is True
    assert summary["privacy"] == {
        "raw_audio_logged": False,
        "transcript_text_logged": False,
        "hashes_only": True,
    }
    assert raw_text not in payload
    assert raw_reference not in payload
    assert "text_sha256_12" in payload
    assert "text_words" in payload
    assert summary["coverage"]["reference_words"] == 5
    assert summary["quality_gate"]["failures"] == []


def test_summary_redacts_url_paths_and_upstream_error_values(tmp_path: Path) -> None:
    smoke = _load_smoke_module()
    secret = "do-not-persist-this-secret"
    user_path = tmp_path / "private-call.wav"
    user_path.write_bytes(b"privacy-fixture")
    started_at = time.perf_counter()

    with pytest.raises(smoke.SmokeError, match="userinfo"):
        smoke.validate_stream_url(
            f"wss://user:{secret}@example.test/ws/stream?protocol=source-ranges-v1"
        )
    with pytest.raises(smoke.SmokeError, match="negotiate"):
        smoke.validate_stream_url(
            f"wss://example.test/ws/stream?protocol=source-ranges-v1&access_token={secret}"
        )

    summary = smoke.build_summary(
        url=f"wss://example.test/ws/stream?protocol=source-ranges-v1&access_token={secret}",
        wav_path=user_path,
        audio_samples=16_000,
        started_at=started_at,
        loading_events=["loading:live_model", "loading:final_model"],
        ready_at=started_at + 0.1,
        transcript_events=[],
        terminal_events=[],
        errors=["upstream_error"],
    )
    payload = json.dumps(summary, ensure_ascii=False)

    assert summary["url"] == "wss://example.test/ws/stream"
    assert secret not in payload
    assert "private-call.wav" not in payload
    assert summary["fixture"]["artifact_id_sha256_12"]


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        ("contract", "smoke_contract_failed"),
        ("internal", "smoke_internal_failed"),
    ],
)
def test_main_redacts_failure_stderr_and_exception_details(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
    expected_code: str,
) -> None:
    smoke = _load_smoke_module()
    secret = "/Users/private/tenant-a/meeting.wav?access_token=do-not-log"

    async def fail(_args: object) -> dict[str, object]:
        if failure == "contract":
            raise smoke.SmokeError(secret)
        raise RuntimeError(secret)

    monkeypatch.setattr(smoke, "run_smoke", fail)

    assert smoke.main([]) == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert payload == {
        "schema": "platform-ai.live-stt.stream-smoke.error.v1",
        "ok": False,
        "error_code": expected_code,
    }
    assert captured.err == ""
    assert secret not in captured.out
    assert "Traceback" not in captured.out


def test_main_retrieves_concurrent_receiver_failure_without_stderr_leak(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    smoke = _load_smoke_module()
    secret = "/Users/private/tenant-a/meeting.wav?access_token=do-not-log"
    events = [
        {"type": "loading", "stage": "live_model"},
        {"type": "loading", "stage": "final_model"},
        {
            "type": "ready",
            "sample_rate": 16_000,
            "live_model": "fixture-live",
            "final_model": "fixture-final",
            "partial_mode": "stable-v1",
            "protocol": "source-ranges-v1",
            "capabilities": ["eof", "source-ranges-v1"],
            "supports_eof": True,
            "terminal_timeout_ms": 60_000,
        },
        {"type": "unexpected"},
    ]

    class FakeWebsocket:
        async def recv(self) -> str:
            await asyncio.sleep(0)
            return json.dumps(events.pop(0))

        async def send(self, payload: bytes | str) -> None:
            if isinstance(payload, bytes):
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                raise RuntimeError(secret)

    class FakeConnection:
        async def __aenter__(self) -> FakeWebsocket:
            return FakeWebsocket()

        async def __aexit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(smoke.websockets, "connect", lambda *_args, **_kwargs: FakeConnection())
    monkeypatch.setattr(
        smoke,
        "load_wav_float32",
        lambda _path: np.ones(16, dtype=np.float32),
    )
    monkeypatch.setattr(
        smoke,
        "audio_frames",
        lambda _audio, **_kwargs: [np.ones(1, dtype=np.float32)],
    )

    assert smoke.main(["--wav", secret, "--tail-silence-sec", "0"]) == 1
    gc.collect()
    captured = capsys.readouterr()

    assert json.loads(captured.out) == {
        "schema": "platform-ai.live-stt.stream-smoke.error.v1",
        "ok": False,
        "error_code": "smoke_internal_failed",
    }
    assert captured.err == ""
    assert secret not in captured.out
    assert "Traceback" not in captured.out


def test_summary_fails_when_final_word_coverage_is_too_low() -> None:
    smoke = _load_smoke_module()
    raw_text = "Eksik"
    started_at = time.perf_counter()

    summary = smoke.build_summary(
        url="ws://127.0.0.1:18220/ws/stream",
        wav_path=FIXTURE,
        audio_samples=88_320,
        started_at=started_at,
        loading_events=["loading:live_model", "loading:final_model"],
        ready_at=started_at + 0.1,
        transcript_events=[
            smoke.redacted_transcript_event(
                {
                    "type": "partial",
                    "seq": 0,
                    "confirmed": "",
                    "tentative": "Eksik",
                    "elapsed_ms": 300,
                    "rms": 0.03,
                },
                450,
            ),
            smoke.redacted_transcript_event(
                {
                    "type": "final",
                    "seq": 0,
                    "text": raw_text,
                    "elapsed_ms": 700,
                    "rms": 0.03,
                },
                1500,
            ),
        ],
        terminal_events=["eof_ack", "drained"],
        errors=[],
        reference_text_path=FIXTURE.with_suffix(".txt"),
        min_final_word_coverage=0.5,
        final_transcript_text=raw_text,
    )
    payload = json.dumps(summary, ensure_ascii=False)

    assert summary["ok"] is False
    assert summary["coverage"]["final_words"] == 1
    assert summary["coverage"]["reference_words"] == 5
    assert summary["coverage"]["final_word_coverage"] == 0.2
    assert summary["coverage"]["reference_token_coverage"] == 0.0
    assert summary["coverage"]["word_error_rate"] == 1.0
    assert "final_word_coverage_below_min" in summary["quality_gate"]["failures"]
    assert raw_text not in payload


def test_summary_rejects_equal_length_but_unrelated_final_text() -> None:
    smoke = _load_smoke_module()
    unrelated = "Tamamen ilgisiz beş sözcüklü sonuç burada"
    started_at = time.perf_counter()

    summary = smoke.build_summary(
        url="ws://127.0.0.1:18220/ws/stream",
        wav_path=FIXTURE,
        audio_samples=88_320,
        started_at=started_at,
        loading_events=["loading:live_model", "loading:final_model"],
        ready_at=started_at + 0.1,
        transcript_events=[
            {"type": "partial", "received_at_ms": 100},
            {"type": "final", "received_at_ms": 200, "text_words": 6},
        ],
        terminal_events=["eof_ack", "drained"],
        errors=[],
        reference_text_path=FIXTURE.with_suffix(".txt"),
        final_transcript_text=unrelated,
    )
    payload = json.dumps(summary, ensure_ascii=False)

    assert summary["ok"] is False
    assert "reference_token_coverage_below_min" in summary["quality_gate"]["failures"]
    assert "word_error_rate_above_max" in summary["quality_gate"]["failures"]
    assert unrelated not in payload


def test_summary_rejects_two_missing_reference_words() -> None:
    smoke = _load_smoke_module()
    degraded = "Geçiş ülkelerinde yaşananlar"
    started_at = time.perf_counter()

    summary = smoke.build_summary(
        url="ws://127.0.0.1:18220/ws/stream",
        wav_path=FIXTURE,
        audio_samples=88_320,
        started_at=started_at,
        loading_events=["loading:live_model", "loading:final_model"],
        ready_at=started_at + 0.1,
        transcript_events=[
            {"type": "partial", "received_at_ms": 100, "text_words": 3},
            {"type": "final", "received_at_ms": 200, "text_words": 3},
        ],
        terminal_events=["eof_ack", "drained"],
        errors=[],
        reference_text_path=FIXTURE.with_suffix(".txt"),
        final_transcript_text=degraded,
    )

    assert summary["ok"] is False
    assert summary["coverage"]["reference_token_coverage"] == 0.6
    assert "final_word_coverage_below_min" in summary["quality_gate"]["failures"]
    assert "reference_token_coverage_below_min" in summary["quality_gate"]["failures"]


def test_summary_rejects_missing_terminal_drain() -> None:
    smoke = _load_smoke_module()
    started_at = time.perf_counter()

    summary = smoke.build_summary(
        url="ws://127.0.0.1:18220/ws/stream",
        wav_path=FIXTURE,
        audio_samples=88_320,
        started_at=started_at,
        loading_events=["loading:live_model", "loading:final_model"],
        ready_at=started_at + 0.1,
        transcript_events=[
            {"type": "partial", "received_at_ms": 100},
            {"type": "final", "received_at_ms": 200, "text_words": 5},
        ],
        terminal_events=["eof_ack"],
        errors=[],
        reference_text_path=FIXTURE.with_suffix(".txt"),
    )

    assert summary["ok"] is False
    assert "terminal_sequence_invalid" in summary["quality_gate"]["failures"]


def test_final_event_count_honors_requested_long_smoke_gate() -> None:
    smoke = _load_smoke_module()

    events = [
        {"type": "partial"},
        {"type": "final"},
        {"type": "partial"},
        {"type": "final"},
    ]

    assert smoke.final_event_count(events) == 2
    assert smoke.final_event_count(events) < 3
