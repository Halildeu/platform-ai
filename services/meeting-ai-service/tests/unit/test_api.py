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
    assert body["summary_grounding_status"] in ("verified", "partial_verified")
    assert body["summary_citations"]
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


def test_analyze_ollama_down_502(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import httpx

    def _boom(*a, **k):  # type: ignore[no-untyped-def]
        raise httpx.ConnectError("connection refused")

    monkeypatch.setenv("MAI_BACKEND", "ollama")
    monkeypatch.setattr(httpx, "post", _boom)
    with TestClient(app) as client:
        resp = client.post("/analyze", json={"transcript": "Bir metin."})
    assert resp.status_code == 502


def test_analyze_segments_attach_timestamps(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # #162: STT segments in the request → citations carry wall-clock start_sec
    monkeypatch.setenv("MAI_REDACT_PII", "False")  # mock backend: keep text verbatim
    with TestClient(app) as client:
        resp = client.post(
            "/analyze",
            json={
                "transcript": "Bütçe artışı onaylandı. Ali raporu hazırlayacak.",
                "segments": [
                    {"text": "Bütçe artışı onaylandı.", "start": 0.0, "end": 3.0},
                    {"text": "Ali raporu hazırlayacak.", "start": 3.0, "end": 6.0},
                ],
            },
        )
    assert resp.status_code == 200
    citations = resp.json()["citations"]
    grounded = [c for c in citations if c["grounded"]]
    assert grounded, "expected at least one grounded citation"
    assert all(c["start_sec"] is not None for c in grounded)


def test_analyze_without_segments_has_no_timestamps() -> None:
    with TestClient(app) as client:
        resp = client.post(
            "/analyze",
            json={"transcript": "Bütçe artışı onaylandı. Ali raporu hazırlayacak."},
        )
    assert resp.status_code == 200
    assert all(c["start_sec"] is None for c in resp.json()["citations"])


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


def test_analyze_nonmock_residual_pii_blocked_422(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # ADR-0043 D3 fail-closed: a non-mock backend + PII that survives precise redaction
    # (a 0-leading 11-digit, missed by the TC/phone patterns) → 422; the LLM is NEVER
    # called (the residual gate runs before the analyzer). No network dependency.
    monkeypatch.setenv("MAI_BACKEND", "ollama")
    with TestClient(app) as client:
        resp = client.post("/analyze", json={"transcript": "Kayıt 01234567890 girildi."})
    assert resp.status_code == 422
