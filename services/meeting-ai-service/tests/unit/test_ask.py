"""ask.py + /ask endpoint tests — grounded answers + hallucination guard."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import app
from app.services.ask import answer_question

TRANSCRIPT = (
    "Bütçe artışı yönetim kurulunda onaylandı. "
    "Ali raporu cuma gününe kadar hazırlayacak. "
    "Bir sonraki toplantı pazartesi yapılacak."
)


def _settings(**kw: object) -> Settings:
    return Settings(backend="mock", redact_pii=False, **kw)  # type: ignore[arg-type]


def test_mock_answer_is_grounded() -> None:
    r = answer_question(TRANSCRIPT, "Toplantı ne zaman?", _settings())
    assert r.grounded is True
    assert "pazartesi" in r.answer.lower()
    assert r.citation.grounded is True
    assert r.citation.source_index >= 0


def test_answer_cites_correct_sentence() -> None:
    r = answer_question(TRANSCRIPT, "Bütçe ne oldu?", _settings())
    assert "onaylandı" in r.answer.lower()
    assert "Bütçe" in r.citation.source_text


def test_endpoint_returns_answer() -> None:
    with TestClient(app) as client:
        resp = client.post(
            "/ask", json={"transcript": TRANSCRIPT, "question": "Raporu kim hazırlayacak?"}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert "Ali" in body["answer"]
    assert body["grounded"] is True


def test_endpoint_ollama_down_502(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*a: object, **k: object) -> httpx.Response:
        raise httpx.ConnectError("refused")

    monkeypatch.setenv("MAI_BACKEND", "ollama")
    monkeypatch.setattr(httpx, "post", _boom)
    with TestClient(app) as client:
        resp = client.post("/ask", json={"transcript": TRANSCRIPT, "question": "Ne karar verildi?"})
    assert resp.status_code == 502
