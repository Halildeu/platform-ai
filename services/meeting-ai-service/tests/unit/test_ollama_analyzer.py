"""OllamaAnalyzer tests — Option B backend (#54), httpx mocked (no real Ollama)."""

from __future__ import annotations

import json

import httpx
import pytest

from app.core.config import Settings
from app.services.analyze import (
    BackendUnavailableError,
    OllamaAnalyzer,
    OllamaSchemaInvalidError,
    OllamaUnparseableOutputError,
)


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


def test_ollama_invalid_json_raises_unparseable(monkeypatch: pytest.MonkeyPatch) -> None:
    # Not even valid JSON → model format-contract failure (format_invalid), distinct
    # from a schema violation and from an infra error (Codex review).
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _ollama_response("not json at all"))
    with pytest.raises(OllamaUnparseableOutputError) as excinfo:
        OllamaAnalyzer(_settings()).analyze("redacted transcript")
    assert not isinstance(excinfo.value, OllamaSchemaInvalidError)


def test_ollama_malformed_items_raise_backend_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # action_items entries that are not objects are a schema violation
    payload = {"summary": "x", "decisions": [], "action_items": ["plain string"]}
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _ollama_response(payload))
    with pytest.raises(OllamaSchemaInvalidError):
        OllamaAnalyzer(_settings()).analyze("redacted transcript")


def test_backend_error_message_is_transcript_free(monkeypatch: pytest.MonkeyPatch) -> None:
    # KVKK: the raised message must never echo transcript content.
    secret = "çok gizli toplantı metni"
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _ollama_response("not json"))
    with pytest.raises(BackendUnavailableError) as excinfo:
        OllamaAnalyzer(_settings()).analyze(secret)
    assert secret not in str(excinfo.value)


def _capture_post(captured: dict[str, object]):
    def _post(*a: object, **k: object) -> httpx.Response:
        captured.update(k.get("json", {}))  # type: ignore[arg-type]
        return _ollama_response({"summary": "x", "decisions": [], "action_items": []})

    return _post


def test_ollama_sends_deterministic_decoding_options(monkeypatch: pytest.MonkeyPatch) -> None:
    # Fair/reproducible-eval fix (#162): the request MUST pin num_ctx (so long
    # transcripts are not truncated to Ollama's 2048 default), temperature (greedy,
    # not chat 0.8), format=json, keep_alive, and a seed when one is configured.
    captured: dict[str, object] = {}
    monkeypatch.setattr(httpx, "post", _capture_post(captured))
    settings = _settings(ollama_num_ctx=16384, ollama_temperature=0.0, ollama_seed=7)
    OllamaAnalyzer(settings).analyze("redacted transcript")

    assert captured["format"] == "json"
    assert captured["keep_alive"] == settings.ollama_keep_alive
    opts = captured["options"]
    assert isinstance(opts, dict)
    assert opts["num_ctx"] == 16384  # not the truncating 2048 default
    assert opts["temperature"] == 0.0  # deterministic extraction
    assert opts["seed"] == 7  # reproducible when set


def test_ollama_options_omit_seed_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    # No seed configured → omit it (Ollama uses a random seed), matching defaults.
    captured: dict[str, object] = {}
    monkeypatch.setattr(httpx, "post", _capture_post(captured))
    OllamaAnalyzer(_settings()).analyze("redacted transcript")
    opts = captured["options"]
    assert isinstance(opts, dict)
    assert "seed" not in opts


def test_ollama_missing_field_is_schema_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    # Missing "decisions" → schema violation, NOT a silent "no decisions" (Codex):
    # the bakeoff must distinguish a contract break from genuine low recall.
    payload = {"summary": "x", "action_items": []}  # decisions key absent
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _ollama_response(payload))
    with pytest.raises(OllamaSchemaInvalidError):
        OllamaAnalyzer(_settings()).analyze("redacted transcript")


def test_ollama_wrong_type_is_schema_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    # "decisions" as a string must NOT silently become a per-character list.
    payload = {"summary": "x", "decisions": "tek karar", "action_items": []}
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _ollama_response(payload))
    with pytest.raises(OllamaSchemaInvalidError):
        OllamaAnalyzer(_settings()).analyze("redacted transcript")


def test_ollama_decision_object_is_schema_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    # decisions must be list[str] — an object item is a contract break (Codex review).
    payload = {"summary": "x", "decisions": [{"text": "karar"}], "action_items": []}
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _ollama_response(payload))
    with pytest.raises(OllamaSchemaInvalidError):
        OllamaAnalyzer(_settings()).analyze("redacted transcript")


def test_ollama_action_text_nonstr_is_schema_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"summary": "x", "decisions": [], "action_items": [{"text": ["a"], "owner": None}]}
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _ollama_response(payload))
    with pytest.raises(OllamaSchemaInvalidError):
        OllamaAnalyzer(_settings()).analyze("redacted transcript")


def test_ollama_action_owner_nonstr_is_schema_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"summary": "x", "decisions": [], "action_items": [{"text": "a", "owner": 42}]}
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _ollama_response(payload))
    with pytest.raises(OllamaSchemaInvalidError):
        OllamaAnalyzer(_settings()).analyze("redacted transcript")


def test_ollama_infra_error_is_not_schema_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    # A host/HTTP failure is BackendUnavailableError but NOT the schema subclass, so
    # the bakeoff separates infra failures (backend_error) from model contract breaks.
    def _boom(*a: object, **k: object) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "post", _boom)
    with pytest.raises(BackendUnavailableError) as excinfo:
        OllamaAnalyzer(_settings()).analyze("redacted transcript")
    # pure infra: neither a schema nor a format (model-contract) failure
    assert not isinstance(excinfo.value, OllamaSchemaInvalidError)
    assert not isinstance(excinfo.value, OllamaUnparseableOutputError)
