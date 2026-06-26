"""ask.py + /ask endpoint tests — grounded answers + hallucination guard."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import app
from app.services.ask import answer_question
from app.services.redact import RedactionError

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


def test_mock_unrelated_question_returns_no_info_without_source_sentence() -> None:
    result = answer_question(TRANSCRIPT, "Yeni ofis nerede açıldı?", _settings())
    assert result.answer == "Metinde bu bilgi yok."
    assert result.grounded is False
    assert result.citation.source_index == -1


def test_endpoint_ollama_down_502(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*a: object, **k: object) -> httpx.Response:
        raise httpx.ConnectError("refused")

    monkeypatch.setenv("MAI_BACKEND", "ollama")
    monkeypatch.setattr(httpx, "post", _boom)
    with TestClient(app) as client:
        resp = client.post("/ask", json={"transcript": TRANSCRIPT, "question": "Ne karar verildi?"})
    assert resp.status_code == 502


def test_endpoint_ask_nonmock_residual_transcript_pii_blocked_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAI_BACKEND", "ollama")
    with TestClient(app) as client:
        resp = client.post(
            "/ask",
            json={"transcript": "Kayıt 01234567890 girildi.", "question": "Ne girildi?"},
        )
    assert resp.status_code == 422
    assert "01234567890" not in resp.text


def test_endpoint_ask_nonmock_residual_question_pii_blocked_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAI_BACKEND", "ollama")
    with TestClient(app) as client:
        resp = client.post(
            "/ask",
            json={"transcript": TRANSCRIPT, "question": "01234567890 hangi kayda ait?"},
        )
    assert resp.status_code == 422
    assert "01234567890" not in resp.text


def test_answer_question_blocks_residual_question_before_llm() -> None:
    settings = Settings(backend="ollama")
    with pytest.raises(RedactionError):
        answer_question(TRANSCRIPT, "01234567890 hangi kayda ait?", settings)


def test_endpoint_ask_llm_backend_501(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAI_BACKEND", "anthropic")
    with TestClient(app) as client:
        resp = client.post("/ask", json={"transcript": TRANSCRIPT, "question": "Ne oldu?"})
    assert resp.status_code == 501


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
    result = answer_question(TRANSCRIPT, "Toplantı ne zaman?", settings)

    assert "format" not in captured  # ask returns prose, not a JSON object
    assert captured["keep_alive"] == settings.ollama_keep_alive
    opts = captured["options"]
    assert isinstance(opts, dict)
    assert opts["num_ctx"] == 16384
    assert opts["temperature"] == 0.0
    assert result.answer == "Toplantı pazartesi yapılacak."
    assert result.grounded is True


def test_ask_ollama_redacts_question_before_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def _capture(*a: object, **k: object) -> httpx.Response:
        captured.update(k.get("json", {}))  # type: ignore[arg-type]
        return httpx.Response(
            200,
            json={"response": "Metinde bu bilgi yok."},
            request=httpx.Request("POST", "http://localhost:11434/api/generate"),
        )

    monkeypatch.setattr(httpx, "post", _capture)
    settings = Settings(backend="ollama")
    answer_question(TRANSCRIPT, "ali@example.com hangi aksiyona bağlı?", settings)

    prompt = captured["prompt"]
    assert isinstance(prompt, str)
    assert "ali@example.com" not in prompt
    assert "***REDACTED_EMAIL***" in prompt


def test_ask_ollama_ungrounded_answer_is_withheld(monkeypatch: pytest.MonkeyPatch) -> None:
    def _hallucinate(*a: object, **k: object) -> httpx.Response:
        return httpx.Response(
            200,
            json={"response": "Şirket yeni fabrika açtı."},
            request=httpx.Request("POST", "http://localhost:11434/api/generate"),
        )

    monkeypatch.setattr(httpx, "post", _hallucinate)
    settings = Settings(backend="ollama")
    result = answer_question(TRANSCRIPT, "Şirket ne açtı?", settings)

    assert result.answer == "Metinde bu bilgi yok."
    assert result.grounded is False
    assert result.citation.source_index == -1
    assert result.citation.claim == ""
    assert "fabrika" not in result.answer.lower()


def test_ask_ollama_no_info_sentinel_keeps_fixed_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _no_info(*a: object, **k: object) -> httpx.Response:
        return httpx.Response(
            200,
            json={"response": "Metinde bu bilgi yok."},
            request=httpx.Request("POST", "http://localhost:11434/api/generate"),
        )

    monkeypatch.setattr(httpx, "post", _no_info)
    result = answer_question(TRANSCRIPT, "Şirket ne açtı?", Settings(backend="ollama"))

    assert result.answer == "Metinde bu bilgi yok."
    assert result.grounded is False
    assert result.citation.reason == "answer does not claim transcript support"
