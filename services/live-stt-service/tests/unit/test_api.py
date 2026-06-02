"""HTTP API tests — health + /transcribe (mocked Whisper)."""

from __future__ import annotations


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
