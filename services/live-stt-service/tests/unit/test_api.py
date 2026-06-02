"""HTTP API tests — health + /transcribe (mocked Whisper).

Codex `019e877b` iter-1 absorb: added threadpool/timeout/error-class coverage.
"""

from __future__ import annotations

import time

import pytest


def test_health_loading_before_first_request(client) -> None:  # type: ignore[no-untyped-def]
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "loading"
    assert body["model"] == "tiny"
    assert body["device"] == "cpu"


def test_transcribe_happy_path(client) -> None:  # type: ignore[no-untyped-def]
    audio = b"FAKE_AUDIO_BYTES" * 100
    r = client.post(
        "/transcribe",
        files={"audio": ("clip.wav", audio, "audio/wav")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["text"] == "Merhaba dünya. Toplantı başlıyor."
    assert body["language"] == "tr"
    assert body["duration"] == 2.5
    assert body["model"] == "tiny"
    assert body["device"] == "cpu"
    assert len(body["segments"]) == 2
    assert body["segments"][0]["start"] == 0.0
    assert body["segments"][0]["end"] == 1.2


def test_health_ok_after_transcribe(client) -> None:  # type: ignore[no-untyped-def]
    audio = b"FAKE" * 10
    client.post("/transcribe", files={"audio": ("c.wav", audio, "audio/wav")})
    r = client.get("/health")
    assert r.json()["status"] == "ok"


def test_transcribe_empty_body_rejected(client) -> None:  # type: ignore[no-untyped-def]
    r = client.post("/transcribe", files={"audio": ("c.wav", b"", "audio/wav")})
    assert r.status_code == 400
    assert "Empty audio" in r.json()["detail"]


def test_transcribe_oversize_rejected(client) -> None:  # type: ignore[no-untyped-def]
    # STT_MAX_AUDIO_MB=10 in test fixture
    big = b"X" * (11 * 1024 * 1024)
    r = client.post("/transcribe", files={"audio": ("c.wav", big, "audio/wav")})
    assert r.status_code == 413
    assert "> limit" in r.json()["detail"]


def test_transcribe_bad_content_type_rejected(client) -> None:  # type: ignore[no-untyped-def]
    r = client.post(
        "/transcribe",
        files={"audio": ("c.txt", b"hello", "text/plain")},
    )
    assert r.status_code == 400
    assert "Unsupported content_type" in r.json()["detail"]


def test_openapi_exposes_endpoints(client) -> None:  # type: ignore[no-untyped-def]
    r = client.get("/openapi.json")
    assert r.status_code == 200
    paths = r.json()["paths"]
    assert "/health" in paths
    assert "/transcribe" in paths


# ─── Codex iter-1 absorb: timeout + error-class coverage ────────────────────


def _force_service_error(client, exc_to_raise: BaseException, sleep_for: float = 0.0):  # type: ignore[no-untyped-def]
    """Patch the live TranscribeService.transcribe to raise / sleep."""
    from app.core.config import get_settings
    from app.services.transcribe import get_service

    svc = get_service(get_settings())

    def boom(_audio):  # type: ignore[no-untyped-def]
        if sleep_for > 0:
            time.sleep(sleep_for)
        if exc_to_raise is not None:
            raise exc_to_raise
        raise AssertionError("test setup error — no exception or sleep")

    svc.transcribe = boom  # type: ignore[method-assign]


def test_transcribe_timeout_504(client, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Inference exceeding STT_REQUEST_TIMEOUT → 504."""
    monkeypatch.setenv("STT_REQUEST_TIMEOUT", "1")
    from app.core import config as cfg

    cfg._settings = None

    _force_service_error(client, exc_to_raise=None, sleep_for=2.0)  # 2s > 1s timeout

    r = client.post("/transcribe", files={"audio": ("c.wav", b"AUDIO" * 50, "audio/wav")})
    assert r.status_code == 504
    assert "timeout" in r.json()["detail"].lower()


@pytest.mark.parametrize(
    "exc, expected_status, expected_fragment",
    [
        (RuntimeError("ffmpeg decode err"), 400, "decode or inference"),
        (ValueError("bad sample rate"), 400, "decode or inference"),
        (MemoryError("oom"), 503, "memory"),
        (OSError("read failed"), 500, "I/O"),
    ],
)
def test_transcribe_error_mapping(  # type: ignore[no-untyped-def]
    client, exc: BaseException, expected_status: int, expected_fragment: str
) -> None:
    """Distinct exception classes map to correct HTTP status."""
    _force_service_error(client, exc_to_raise=exc, sleep_for=0.0)

    r = client.post("/transcribe", files={"audio": ("c.wav", b"AUDIO" * 50, "audio/wav")})
    assert r.status_code == expected_status, r.text
    assert expected_fragment.lower() in r.json()["detail"].lower()


def test_transcribe_error_message_sanitized(client) -> None:  # type: ignore[no-untyped-def]
    """Raw exception str() must NOT be echoed (PII leak guard)."""
    secret_path = "/private/recording/customer-name-leak.wav"
    _force_service_error(client, exc_to_raise=RuntimeError(secret_path))

    r = client.post("/transcribe", files={"audio": ("c.wav", b"X" * 50, "audio/wav")})
    assert r.status_code == 400
    # The endpoint must surface only the exception *class name*, never the path.
    assert secret_path not in r.json()["detail"]
    assert "RuntimeError" in r.json()["detail"]
