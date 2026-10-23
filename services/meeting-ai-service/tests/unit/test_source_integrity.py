"""Source coverage and transport rejection, independent of model task accuracy."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings
from app.main import app
from app.services.analyze import BackendUnavailableError, MeetingAnalysisService, OllamaAnalyzer
from app.services.ask import answer_question
from app.services.ollama_runtime import generate

MODEL = "llama3.1:8b"
DIGEST = "46e0c10c039e019119339687c3c1757cc81b9da49709a3b3924863ba87ca666e"
EMPTY = {"summary_sentences": [], "decision_sentences": [], "action_item_sentences": []}


def settings(**kwargs: object) -> Settings:
    return Settings(
        backend="ollama",
        ollama_expected_digest=DIGEST,
        ollama_source_integrity=True,
        **kwargs,  # type: ignore[arg-type]
    )


def envelope(selection: object = EMPTY, **overrides: object) -> dict[str, object]:
    return {
        "model": MODEL,
        "done": True,
        "done_reason": "stop",
        "response": json.dumps(selection),
        **overrides,
    }


class Runtime:
    def __init__(self, responses: list[dict[str, object]] | None = None) -> None:
        self.responses = iter(responses or [envelope()])
        self.payloads: list[dict[str, object]] = []
        self.version: object = "0.34.4"
        self.architecture: object = "llama"
        self.format: object = "gguf"
        self.digest: object = DIGEST
        self.after_version: object = "0.34.4"

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/version":
            body = {"version": self.after_version if self.payloads else self.version}
        elif path == "/api/tags":
            body = {"models": [{"name": MODEL, "digest": self.digest}]}
        elif path == "/api/show":
            body = {
                "details": {"format": self.format},
                "model_info": {"general.architecture": self.architecture},
            }
        elif path == "/api/generate":
            self.payloads.append(json.loads(request.content))
            body = next(self.responses)
        else:
            raise AssertionError(path)
        return httpx.Response(200, json=body)


def test_old_omitted_task_is_reconsidered_without_losing_cancellation_context() -> None:
    task = "Aylin haftalık raporu hazırlayacak."
    cancelled = "Deniz bağlantıyı kontrol edecek. Deniz için bağlantı kontrolünü iptal ettik."
    old = (
        task
        + " "
        + cancelled
        + " Zeynep. "
        + " ".join(f"Ekip {i} numaralı gündemi görüştü." for i in range(12))
    )
    rt = Runtime(
        [
            envelope(),
            envelope(
                {
                    **EMPTY,
                    "action_item_sentences": [{"sentence": 1, "owner": "Aylin", "due_date": None}],
                }
            ),
        ]
    )
    with httpx.Client(transport=httpx.MockTransport(rt.handle)) as client:
        service = MeetingAnalysisService(
            settings(), analyzer=OllamaAnalyzer(settings(), client=client)
        )
        first = service.analyze(old, live=True)
        assert first.action_items == []
        assert first.live_cursor is not None and first.live_cursor.active_indices == []
        next_source = old + " Zeynep’in teslim saati 10 değil. 11 olacak."
        second = service.analyze(next_source, live=True, live_cursor=first.live_cursor)
    assert second.action_items[0].text == task
    assert second.action_items[0].owner == "Aylin"
    assert second.citations[0].source_index == 0
    assert second.citations[0].source_char_start == 0
    prompt = str(rt.payloads[1]["prompt"])
    assert task in prompt and "bağlantıyı kontrol edecek" in prompt and "iptal ettik" in prompt
    assert "Zeynep." in prompt and "11 olacak." in prompt
    assert prompt.index("bağlantıyı kontrol edecek") < prompt.index("iptal ettik")
    assert "all source sentences" in prompt
    for payload in rt.payloads:
        assert payload["truncate"] is False and payload["shift"] is False
        assert "truncate" not in payload["options"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", "0.34.5"),
        ("version", "0.33.3"),
        ("version", None),
        ("version", {}),
        ("architecture", "qwen3next"),
        ("format", "other"),
        ("digest", "b" * 64),
    ],
)
def test_unqualified_runtime_never_generates(field: str, value: object) -> None:
    rt = Runtime()
    setattr(rt, field, value)
    with httpx.Client(transport=httpx.MockTransport(rt.handle)) as client:
        analyzer = OllamaAnalyzer(settings(), client=client)
        assert analyzer.model_loaded is False
        with pytest.raises(BackendUnavailableError):
            analyzer.analyze("Aylin raporu hazırlayacak.")
    assert rt.payloads == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"done": False},
        {"done": 1},
        {"done_reason": "length"},
        {"done_reason": None},
        {"model": "other"},
        {"model": []},
        {"error": "private content"},
        {"response": None},
    ],
)
def test_partial_or_unverified_json_is_not_success_even_if_selection_is_valid(
    overrides: dict[str, object]
) -> None:
    rt = Runtime([envelope(**overrides)])
    with (
        httpx.Client(transport=httpx.MockTransport(rt.handle)) as client,
        pytest.raises(BackendUnavailableError) as error,
    ):
        OllamaAnalyzer(settings(), client=client).analyze_live("Aylin raporu hazırlayacak.", None)
    assert "private content" not in str(error.value)
    assert len(rt.payloads) == 1


def test_runtime_version_drift_withholds_successful_model_answer() -> None:
    rt = Runtime()
    rt.after_version = "0.34.5"
    with (
        httpx.Client(transport=httpx.MockTransport(rt.handle)) as client,
        pytest.raises(BackendUnavailableError),
    ):
        OllamaAnalyzer(settings(), client=client).analyze("Aylin raporu hazırlayacak.")
    assert len(rt.payloads) == 1


def test_context_overflow_returns_failure_without_new_cursor_or_empty_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rt = Runtime()

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/generate":
            return httpx.Response(400, json={"error": "private content exceeds context"})
        return rt.handle(request)

    monkeypatch.setenv("MAI_BACKEND", "ollama")
    monkeypatch.setenv("MAI_OLLAMA_SOURCE_INTEGRITY", "true")
    monkeypatch.setenv("MAI_OLLAMA_EXPECTED_DIGEST", DIGEST)
    monkeypatch.setattr(
        "app.main.create_ollama_client", lambda: httpx.Client(transport=httpx.MockTransport(handle))
    )
    with TestClient(app) as client:
        result = client.post(
            "/analyze/live", json={"transcript": "Aylin raporu hazırlayacak.", "segment_seq": 2}
        )
    assert result.status_code == 502
    assert "live_cursor" not in result.json() and "action_items" not in result.json()
    assert "private content" not in result.text


@pytest.mark.parametrize(
    "bad", [{"model": MODEL, "stream": True}, {"model": "other", "stream": False}]
)
def test_unqualified_request_cannot_bypass_configured_profile(bad: dict[str, object]) -> None:
    rt = Runtime()
    with (
        httpx.Client(transport=httpx.MockTransport(rt.handle)) as client,
        pytest.raises(httpx.RequestError),
    ):
        generate(settings(), bad, client=client)
    assert rt.payloads == []


def test_source_mode_is_explicit_and_requires_identity_pin() -> None:
    assert Settings().ollama_source_integrity is False
    assert Settings().effective_prompt_version == "mock-v1"
    assert settings().effective_prompt_version == "ollama-complete-source-v1"
    with pytest.raises(ValidationError):
        Settings(backend="ollama", ollama_source_integrity=True)
    with pytest.raises(ValidationError):
        Settings(backend="mock", ollama_source_integrity=True, ollama_expected_digest=DIGEST)


@pytest.mark.parametrize("terminal", ["stop", "length"])
def test_follow_up_question_transport_uses_same_complete_source_gate(
    terminal: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = Runtime([envelope(response="Aylin raporu hazırlayacak.", done_reason=terminal)])
    with httpx.Client(transport=httpx.MockTransport(rt.handle)) as client:
        monkeypatch.setattr(httpx, "get", client.get)
        monkeypatch.setattr(httpx, "post", client.post)
        if terminal == "length":
            with pytest.raises(httpx.RequestError):
                answer_question("Aylin raporu hazırlayacak.", "Kim hazırlayacak?", settings())
        else:
            answer_question("Aylin raporu hazırlayacak.", "Kim hazırlayacak?", settings())
    assert rt.payloads[0]["truncate"] is False and rt.payloads[0]["shift"] is False
