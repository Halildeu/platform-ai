"""OllamaAnalyzer tests — Option B backend (#54), httpx mocked (no real Ollama)."""

from __future__ import annotations

import json

import httpx
import pytest
from prometheus_client import REGISTRY

from app.core.config import Settings
from app.services.analyze import (
    BackendUnavailableError,
    MeetingAnalysisService,
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
        "action_items": [{"text": "Rapor hazırlanacak", "owner": "Ali", "due_date": "cuma"}],
    }
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _ollama_response(payload))
    draft = OllamaAnalyzer(_settings()).analyze("redacted transcript")
    assert draft.summary == "Bütçe görüşüldü."
    assert draft.decisions == ["Bütçe artışı onaylandı."]
    assert draft.action_items[0].owner == "Ali"
    assert draft.action_items[0].due_date == "cuma"


def test_ollama_strips_markdown_fences(monkeypatch: pytest.MonkeyPatch) -> None:
    fenced = '```json\n{"summary": "Özet.", "decisions": [], "action_items": []}\n```'
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _ollama_response(fenced))
    draft = OllamaAnalyzer(_settings()).analyze("redacted transcript")
    assert draft.summary == "Özet."


@pytest.mark.parametrize("duration", [2_000_000_000, None, -1, True, "private text", 2**64])
def test_ollama_records_only_valid_optional_timing_metadata(
    monkeypatch: pytest.MonkeyPatch, duration: object
) -> None:
    metric = "mai_ollama_stage_seconds_sum"
    before = REGISTRY.get_sample_value(metric, {"stage": "load"}) or 0.0
    response = httpx.Response(
        200,
        json={
            "response": json.dumps({"summary": "x", "decisions": [], "action_items": []}),
            "load_duration": duration,
        },
        request=httpx.Request("POST", "http://localhost:11434/api/generate"),
    )
    monkeypatch.setattr(httpx, "post", lambda *a, **k: response)
    assert OllamaAnalyzer(_settings()).analyze("synthetic transcript").summary == "x"
    expected = 2.0 if type(duration) is int and duration == 2_000_000_000 else 0.0
    assert (REGISTRY.get_sample_value(metric, {"stage": "load"}) or 0.0) == before + expected


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

    schema = captured["format"]
    assert isinstance(schema, dict)
    assert schema["type"] == "object"
    assert schema["properties"]["decision_sentences"]["items"]["maximum"] == 1
    assert schema["$defs"]["SelectedAction"]["required"] == ["sentence", "owner", "due_date"]
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


def test_ollama_prompt_requires_extractive_summary_and_independent_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(httpx, "post", _capture_post(captured))
    transcript = (
        "Raporun cuma gününe kadar tamamlanmasına karar verdik ve "
        "bu görevi birinci ekip üstlenecek."
    )

    OllamaAnalyzer(_settings()).analyze(transcript)

    prompt = captured["prompt"]
    assert isinstance(prompt, str)
    # gitops#3444: extractive is no longer *requested* in prose (the model
    # ignored that — measured median coverage 0.33), it is STRUCTURAL: the
    # prompt offers a numbered menu and asks for numbers, so the answer cannot
    # contain model-authored prose at all.
    assert "sadece NUMARA SEÇ" in prompt
    assert "summary_sentences" in prompt
    assert "decision_sentences" in prompt
    assert "action_item_sentences" in prompt
    # The independent decision/action intent of the original test survives.
    assert "HER İKİ listeye de yaz" in prompt
    # The transcript is present as a numbered menu, not as a raw blob.
    assert f"[1] {transcript}" in prompt


def test_selection_prompt_has_no_fabricated_example_indices_or_assignments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(httpx, "post", _capture_post(captured))
    OllamaAnalyzer(_settings()).analyze("Tasarım raporunu ayrıntılı biçimde inceledim.")
    prompt = str(captured["prompt"])
    template = json.loads(prompt[prompt.rfind("{") :])
    assert template == {
        "summary_sentences": [],
        "decision_sentences": [],
        "action_item_sentences": [],
    }
    assert "NOT a current decision" in prompt
    assert "Proposals" in prompt
    assert "owner null" in prompt
    assert "cancelled later" in prompt


def test_empty_semantic_selection_does_not_invent_decisions_from_grounded_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {"summary_sentences": [1], "decision_sentences": [], "action_item_sentences": []}
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _ollama_response(payload))
    transcript = "Tasarım raporunu ayrıntılı biçimde inceledim."
    result = MeetingAnalysisService(_settings()).analyze(transcript)
    assert result.summary == transcript
    assert result.decisions == []
    assert result.action_items == []


@pytest.mark.parametrize("owner", ["ben", "Ben", "biz", "BEN", "we", "null"])
def test_grounded_pronoun_is_not_an_identified_assignee(
    monkeypatch: pytest.MonkeyPatch, owner: str
) -> None:
    transcript = f"Raporu hazırlama görevini {owner} üstleniyorum."
    payload = {
        "summary_sentences": [1],
        "decision_sentences": [],
        "action_item_sentences": [{"sentence": 1, "owner": owner, "due_date": None}],
    }
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _ollama_response(payload))
    result = MeetingAnalysisService(_settings()).analyze(transcript)
    assert len(result.action_items) == 1
    assert result.action_items[0].owner is None
    assert any(rejected.kind == "action_owner" for rejected in result.rejected_claims)


@pytest.mark.parametrize(
    "actions",
    [[2], [{"sentence_number": 2}], [{"sentence": True, "owner": None, "due_date": None}]],
)
def test_malformed_selection_is_a_schema_error_not_successful_empty_actions(
    monkeypatch: pytest.MonkeyPatch, actions: object
) -> None:
    payload = {"summary_sentences": [1], "decision_sentences": [], "action_item_sentences": actions}
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _ollama_response(payload))
    with pytest.raises(OllamaSchemaInvalidError):
        OllamaAnalyzer(_settings()).analyze("Raporu inceledim. Test ekibi belgeyi hazırlayacak.")


def test_overlapping_decision_action_and_summary_survive_grounding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcript = (
        "Raporun cuma gününe kadar tamamlanmasına karar verdik ve "
        "bu görevi birinci ekip üstlenecek."
    )
    payload = {
        "summary": transcript,
        "decisions": [transcript],
        "action_items": [
            {
                "text": transcript,
                "owner": "birinci ekip",
                "due_date": "cuma gününe kadar",
            }
        ],
    }
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _ollama_response(payload))

    result = MeetingAnalysisService(_settings(redact_pii=True)).analyze(transcript)

    assert result.summary == transcript
    assert result.summary_grounding_status == "verified"
    assert result.decisions == [transcript]
    assert len(result.action_items) == 1
    assert result.action_items[0].text == transcript
    assert result.action_items[0].owner == "birinci ekip"
    assert result.action_items[0].due_date == "cuma gününe kadar"
    assert result.rejected_claims == []


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


def test_ollama_action_due_date_nonstr_is_schema_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "summary": "x",
        "decisions": [],
        "action_items": [{"text": "a", "owner": None, "due_date": 42}],
    }
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
