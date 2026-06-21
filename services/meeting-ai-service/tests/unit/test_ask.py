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


def test_ask_ollama_sends_decoding_options(monkeypatch: pytest.MonkeyPatch) -> None:
    # The ask path must carry the same num_ctx/temperature contract as analyze (no
    # 2048-truncation, deterministic) — but ask returns PROSE, so it must NOT force
    # format=json. (Codex review: ask path needs its own request-options test.)
    captured: dict[str, object] = {}

    def _capture(*a: object, **k: object) -> httpx.Response:
        captured.update(k.get("json", {}))  # type: ignore[arg-type]
        return httpx.Response(
            200,
            json={"response": "Toplantı pazartesi yapılacak."},
            request=httpx.Request("POST", "http://localhost:11434/api/generate"),
        )

    monkeypatch.setattr(httpx, "post", _capture)
    # ollama backend requires redaction on (KVKK boundary validator); the request
    # options are what we assert, so redaction of the transcript is fine here.
    settings = Settings(backend="ollama", ollama_num_ctx=16384, ollama_temperature=0.0)
    answer_question(TRANSCRIPT, "Toplantı ne zaman?", settings)

    assert "format" not in captured  # ask returns prose, not a JSON object
    assert captured["keep_alive"] == settings.ollama_keep_alive
    opts = captured["options"]
    assert isinstance(opts, dict)
    assert opts["num_ctx"] == 16384
    assert opts["temperature"] == 0.0
