from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_health_does_not_load_model(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("FINAL_STT_REDIS_ENABLED", "false")
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "loading"
    assert response.json()["model"] == "large-v3"
