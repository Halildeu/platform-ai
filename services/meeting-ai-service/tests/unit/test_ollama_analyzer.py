"""OllamaAnalyzer tests — Option B backend (#54), httpx mocked (no real Ollama)."""

from __future__ import annotations

import json

import httpx
import pytest

from app.core.config import Settings
from app.services.analyze import BackendUnavailableError, OllamaAnalyzer


def _settings(**kwargs: object) -> Settings:
    return Settings(backend="ollama", **kwargs)  # type: ignore[arg-type]


def _ollama_response(payload: object) -> httpx.Response:
    """Wrap an analysis payload the way Ollama /api/generate returns it."""
    body = payload if isinstance(payload, str) else json.dumps(payload)
    return httpx.Response(
        200,
        json={"response": body},
        request=httpx.Request("POST", "http://localhost:11434/api/generate"),
    )


def test_ollama_parses_valid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "summary": "Bütçe görüşüldü.",
        "decisions": ["Bütçe artışı onaylandı."],
        "action_items": [{"text": "Rapor hazırlanacak", "owner": "Ali"}],
    }
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _ollama_response(payload))
    draft = OllamaAnalyzer(_settings()).analyze("redacted transcript")
    assert draft.summary == "Bütçe görüşüldü."
    assert draft.decisions == ["Bütçe artışı onaylandı."]
    assert draft.action_items[0].owner == "Ali"


def test_ollama_strips_markdown_fences(monkeypatch: pytest.MonkeyPatch) -> None:
    fenced = '```json\n{"summary": "Özet.", "decisions": [], "action_items": []}\n```'
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _ollama_response(fenced))
    draft = OllamaAnalyzer(_settings()).analyze("redacted transcript")
    assert draft.summary == "Özet."


def test_ollama_unreachable_raises_backend_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*a: object, **k: object) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "post", _boom)
    with pytest.raises(BackendUnavailableError):
        OllamaAnalyzer(_settings()).analyze("redacted transcript")


def test_ollama_invalid_json_raises_backend_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _ollama_response("not json at all"))
    with pytest.raises(BackendUnavailableError):
        OllamaAnalyzer(_settings()).analyze("redacted transcript")


def test_ollama_malformed_items_raise_backend_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # action_items entries that are not objects must not crash with a raw error
    payload = {"summary": "x", "decisions": [], "action_items": ["plain string"]}
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _ollama_response(payload))
    with pytest.raises(BackendUnavailableError):
        OllamaAnalyzer(_settings()).analyze("redacted transcript")


def test_backend_error_message_is_transcript_free(monkeypatch: pytest.MonkeyPatch) -> None:
    # KVKK: the raised message must never echo transcript content.
    secret = "çok gizli toplantı metni"
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _ollama_response("not json"))
    with pytest.raises(BackendUnavailableError) as excinfo:
        OllamaAnalyzer(_settings()).analyze(secret)
    assert secret not in str(excinfo.value)
