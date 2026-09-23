"""Pinned tag checks must fail closed for analysis, questions and readiness."""

from __future__ import annotations

import json
from unittest.mock import Mock

import httpx
import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.services import ollama_runtime
from app.services.analyze import BackendUnavailableError, OllamaAnalyzer
from app.services.ask import answer_question

DIGEST = "a" * 64


@pytest.mark.parametrize("changed", [False, True])
def test_shared_transport_still_checks_live_identity_before_and_after(
    changed: bool,
) -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/api/generate":
            return response({"response": "not accepted until the second check"})
        return response(inventory("b" * 64 if changed and len(requests) == 3 else DIGEST))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        if changed:
            with pytest.raises(httpx.RequestError, match="model identity unavailable"):
                ollama_runtime.generate(settings(), {}, client=client)
        else:
            assert ollama_runtime.generate(settings(), {}, client=client).status_code == 200
        assert not client.is_closed
    assert requests == ["/api/tags", "/api/generate", "/api/tags"]
    assert client.is_closed


def settings() -> Settings:
    return Settings(backend="ollama", ollama_expected_digest=DIGEST)


def response(data: object) -> httpx.Response:
    return httpx.Response(200, json=data, request=httpx.Request("GET", "http://localhost/api/tags"))


def inventory(digest: str = DIGEST) -> dict[str, object]:
    return {"models": [{"name": "llama3.1:8b", "digest": digest}]}


@pytest.mark.parametrize("value", ["bad", "A" * 64, "a" * 63, "a" * 64 + "\n"])
def test_invalid_digest_rejected(value: str) -> None:
    with pytest.raises(ValidationError):
        Settings(ollama_expected_digest=value)


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"models": []},
        {"models": {}},
        inventory("b" * 64),
        {"models": [{"name": "other:latest", "digest": DIGEST}]},
        {"models": inventory()["models"] * 2},
        {"models": [{"name": []}]},
    ],
)
def test_missing_mismatched_ambiguous_inventory_never_generates(
    monkeypatch: pytest.MonkeyPatch, data: object
) -> None:
    monkeypatch.setattr(httpx, "get", lambda *a, **k: response(data))
    post = Mock()
    monkeypatch.setattr(httpx, "post", post)
    analyzer = OllamaAnalyzer(settings())
    assert analyzer.model_loaded is False
    with pytest.raises(BackendUnavailableError):
        analyzer.analyze("Test ekibi cuma günü raporu hazırlayacak.")
    post.assert_not_called()


def test_pin_checked_before_and_after_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    get = Mock(return_value=response(inventory()))
    monkeypatch.setattr(httpx, "get", get)
    expected = response({"response": "answer"})
    monkeypatch.setattr(httpx, "post", Mock(return_value=expected))
    assert ollama_runtime.generate(settings(), {"model": "llama3.1:8b"}) is expected
    assert get.call_count == 2
    assert OllamaAnalyzer(settings()).model_loaded is True


def test_tag_changed_during_generation_withholds_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        httpx, "get", Mock(side_effect=[response(inventory()), response(inventory("b" * 64))])
    )
    monkeypatch.setattr(httpx, "post", Mock(return_value=response({"response": "not publishable"})))
    with pytest.raises(httpx.RequestError, match="model identity unavailable"):
        ollama_runtime.generate(settings(), {})


def test_follow_up_questions_cannot_bypass_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "get", Mock(return_value=response(inventory("b" * 64))))
    post = Mock()
    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(httpx.RequestError):
        answer_question("Test ekibi raporu hazırlayacak.", "Raporu kim hazırlayacak?", settings())
    post.assert_not_called()


def test_unpinned_legacy_generation_does_not_require_new_inventory_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get = Mock()
    monkeypatch.setattr(httpx, "get", get)
    monkeypatch.setattr(httpx, "post", Mock(return_value=response({})))
    ollama_runtime.generate(Settings(backend="ollama"), {})
    get.assert_not_called()


def test_pin_checks_cannot_extend_generation_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ollama_runtime.time, "monotonic", Mock(side_effect=[0.0, 0.0, 61.0]))
    monkeypatch.setattr(httpx, "get", Mock(return_value=response(inventory())))
    post = Mock()
    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(httpx.TimeoutException):
        ollama_runtime.generate(settings(), {})
    post.assert_not_called()


def test_missing_default_model_is_not_ready_even_without_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(httpx, "get", Mock(return_value=response({"models": []})))
    assert OllamaAnalyzer(Settings(backend="ollama")).model_loaded is False


def test_inventory_unreachable_is_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "get", Mock(side_effect=httpx.ConnectError("unavailable")))
    assert OllamaAnalyzer(settings()).model_loaded is False


@pytest.mark.parametrize("think", [None, False, True])
def test_thinking_override_is_optional_top_level_and_does_not_mutate_input(
    monkeypatch: pytest.MonkeyPatch, think: bool | None
) -> None:
    post = Mock(return_value=response({}))
    monkeypatch.setattr(httpx, "post", post)
    payload = {"model": "synthetic:latest", "options": {"temperature": 0}}
    ollama_runtime.generate(Settings(backend="ollama", ollama_think=think), payload)
    sent = post.call_args.kwargs["json"]
    assert "think" not in payload
    assert "think" not in sent["options"]
    if think is None:
        assert sent == payload
        assert "think" not in sent
    else:
        assert sent["think"] is think


@pytest.mark.parametrize("workflow", ["analysis", "ask"])
def test_analysis_and_questions_share_explicit_false_wire(
    monkeypatch: pytest.MonkeyPatch, workflow: str
) -> None:
    answer = (
        json.dumps({"summary_sentences": [], "decision_sentences": [], "action_item_sentences": []})
        if workflow == "analysis"
        else "Test ekibi raporu hazırlayacak."
    )
    post = Mock(return_value=response({"response": answer}))
    monkeypatch.setattr(httpx, "post", post)
    config = Settings(backend="ollama", ollama_think=False)
    if workflow == "analysis":
        OllamaAnalyzer(config).analyze("Test ekibi raporu hazırlayacak.")
    else:
        answer_question("Test ekibi raporu hazırlayacak.", "Raporu kim hazırlayacak?", config)
    assert post.call_args.kwargs["json"]["think"] is False


def test_legacy_thinking_default_and_environment_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MAI_OLLAMA_THINK", raising=False)
    assert Settings().ollama_think is None
    monkeypatch.setenv("MAI_OLLAMA_THINK", "false")
    assert Settings().ollama_think is False
    monkeypatch.setenv("MAI_OLLAMA_THINK", "unset")
    with pytest.raises(ValidationError):
        Settings()
