"""Pinned tag checks must fail closed for analysis, questions and readiness."""

from __future__ import annotations

from unittest.mock import Mock

import httpx
import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.services import ollama_runtime
from app.services.analyze import BackendUnavailableError, OllamaAnalyzer
from app.services.ask import answer_question

DIGEST = "a" * 64


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
