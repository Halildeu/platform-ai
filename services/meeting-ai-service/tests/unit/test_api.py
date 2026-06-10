"""API smoke tests via FastAPI TestClient."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_analyze_mock_returns_summary() -> None:
    with TestClient(app) as client:
        resp = client.post(
            "/analyze",
            json={"transcript": "Toplantı başladı. Bütçe kararlaştırıldı. Ali hazırlayacak."},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["backend"] == "mock"
    assert body["redacted"] is True
    assert len(body["summary"]) > 0


def test_analyze_redacts_pii_before_response() -> None:
    with TestClient(app) as client:
        resp = client.post(
            "/analyze",
            json={"transcript": "Ali ali@example.com adresinden gönderecek. Karar verildi."},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["redaction_count"] >= 1
    blob = body["summary"] + " ".join(a["text"] for a in body["action_items"])
    assert "ali@example.com" not in blob


def test_analyze_empty_transcript_422() -> None:
    with TestClient(app) as client:
        resp = client.post("/analyze", json={"transcript": ""})
    assert resp.status_code == 422


def test_analyze_too_large_413(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MAI_MAX_TRANSCRIPT_CHARS", "10")
    with TestClient(app) as client:
        resp = client.post("/analyze", json={"transcript": "x" * 50})
    assert resp.status_code == 413


def test_analyze_llm_backend_501(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MAI_BACKEND", "anthropic")
    with TestClient(app) as client:
        resp = client.post("/analyze", json={"transcript": "Bir metin."})
    assert resp.status_code == 501


def test_health_ok() -> None:
    with TestClient(app) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_metrics_endpoint() -> None:
    with TestClient(app) as client:
        resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "mai_analyze_total" in resp.text
