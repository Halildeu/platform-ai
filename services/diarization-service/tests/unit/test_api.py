"""API smoke tests via FastAPI TestClient."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from tests.conftest import make_wav


def test_diarize_mock_returns_segments() -> None:
    with TestClient(app) as client:
        resp = client.post(
            "/diarize",
            files={"audio": ("a.wav", make_wav(6.0), "audio/wav")},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["backend"] == "mock"
    assert body["num_speakers"] >= 1
    assert len(body["segments"]) >= 1
    assert body["segments"][0]["speaker"].startswith("SPEAKER_")


def test_diarize_empty_body_400() -> None:
    with TestClient(app) as client:
        resp = client.post(
            "/diarize",
            files={"audio": ("a.wav", b"", "audio/wav")},
        )
    assert resp.status_code == 400


def test_diarize_wrong_content_type_400() -> None:
    with TestClient(app) as client:
        resp = client.post(
            "/diarize",
            files={"audio": ("a.txt", b"hello", "text/plain")},
        )
    assert resp.status_code == 400


def test_diarize_pyannote_without_token_fails_fast(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """#48: pyannote backend without DIA_HF_TOKEN fails at config time —
    misconfiguration is caught at startup, not as a runtime 5xx."""
    import pydantic
    import pytest

    from app.core.config import Settings

    monkeypatch.setenv("DIA_BACKEND", "pyannote")
    monkeypatch.delenv("DIA_HF_TOKEN", raising=False)
    with pytest.raises(pydantic.ValidationError):
        Settings()


def test_health_ok() -> None:
    with TestClient(app) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_metrics_endpoint() -> None:
    with TestClient(app) as client:
        resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "dia_diarize_total" in resp.text
