"""Incremental model context preserves grounded state, without a transcript cache."""

import hashlib
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import app
from app.models.schemas import LiveAnalysisCursor
from app.services.analyze import MeetingAnalysisService
from app.services.citation import split_sentences
from app.services.live_context import live_menu


def cursor(text: str, active: list[int]) -> LiveAnalysisCursor:
    return LiveAnalysisCursor(
        source_length=len(text),
        source_sha256=hashlib.sha256(text.encode()).hexdigest(),
        active_indices=active,
    )


def test_menu_keeps_old_active_claim_and_every_new_sentence_but_drops_old_background() -> None:
    old = " ".join(f"Ekip {n} numaralı gündem maddesini görüştü." for n in range(20))
    new = old + " Mehmet raporu hazırlayacak. Sunumu çevrim içi yapmaya karar verdik."
    all_sentences = split_sentences(new)
    selected = live_menu(new, all_sentences, cursor(old, [1]))
    assert [s.index for s in selected] == [1, 17, 18, 19, 20, 21]
    assert selected[-1].text == all_sentences[-1].text
    assert selected[-1].start_char == all_sentences[-1].start_char


@pytest.mark.parametrize("change", ["correction", "trim", "bad_index", "bad_hash"])
def test_any_prefix_drift_or_invalid_index_falls_back_to_whole_source(change: str) -> None:
    text = "Sunum çevrim içi yapılacak. Mehmet raporu hazırlayacak."
    previous = cursor(text, [0])
    if change == "correction":
        text = text.replace("Mehmet", "Ayşe")
    elif change == "trim":
        text = "Mehmet raporu hazırlayacak."
    elif change == "bad_index":
        previous.active_indices = [999]
    else:
        previous.source_sha256 = "0" * 64
    source = split_sentences(text)
    assert live_menu(text, source, previous) == source


def test_expanding_last_sentence_is_always_reconsidered() -> None:
    text = "Sunum çevrim içi yapılacak. Mehmet raporu"
    extended = text + " cuma günü hazırlayacak."
    selected = live_menu(extended, split_sentences(extended), cursor(text, [0]))
    assert selected[-1].text.endswith("cuma günü hazırlayacak.")


def test_live_snapshots_reconsider_cancellation_and_never_copy_an_old_action_blindly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts: list[str] = []
    answers = iter(
        [
            {
                "summary_sentences": [1],
                "decision_sentences": [],
                "action_item_sentences": [{"sentence": 1, "owner": "Mehmet", "due_date": None}],
            },
            {"summary_sentences": [2], "decision_sentences": [], "action_item_sentences": []},
        ]
    )

    def post(*_args: object, **kwargs: object) -> httpx.Response:
        payload = kwargs["json"]
        assert isinstance(payload, dict)
        prompts.append(payload["prompt"])
        return httpx.Response(
            200,
            json={"response": json.dumps(next(answers))},
            request=httpx.Request("POST", "http://localhost/api/generate"),
        )

    monkeypatch.setattr(httpx, "post", post)
    service = MeetingAnalysisService(Settings(backend="ollama"))
    text = "Mehmet bütçe raporunu hazırlayacak."
    first = service.analyze(text, live=True)
    assert first.action_items[0].owner == "Mehmet"
    assert first.live_cursor is not None
    second = service.analyze(
        text + " Mehmet için rapor hazırlama görevini iptal ettik.",
        live=True,
        live_cursor=first.live_cursor,
    )
    assert second.action_items == []
    assert text in prompts[1] and "iptal ettik" in prompts[1]
    assert all(c.grounded for c in first.citations)
    assert first.citations[0].source_char_start == 0
    assert "Mehmet" not in first.live_cursor.model_dump_json()


def test_uninformative_live_speech_does_not_call_model(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("No model call for content-free speech")

    monkeypatch.setattr(httpx, "post", unexpected)
    result = MeetingAnalysisService(Settings(backend="ollama")).analyze("Tamam.", live=True)
    assert not result.decisions and not result.action_items


def test_final_analysis_ignores_incremental_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[str] = []

    def post(*_args: object, **kwargs: object) -> httpx.Response:
        payload = kwargs["json"]
        assert isinstance(payload, dict)
        captured.append(payload["prompt"])
        return httpx.Response(
            200,
            json={
                "response": json.dumps(
                    {"summary_sentences": [], "decision_sentences": [], "action_item_sentences": []}
                )
            },
            request=httpx.Request("POST", "http://localhost/api/generate"),
        )

    monkeypatch.setattr(httpx, "post", post)
    text = " ".join(f"Ekip {n} numaralı gündem maddesini görüştü." for n in range(10))
    result = MeetingAnalysisService(Settings(backend="ollama")).analyze(
        text, live_cursor=cursor(text, [])
    )
    assert "Ekip 0" in captured[0] and "Ekip 9" in captured[0]
    assert result.live_cursor is None


def test_live_endpoint_threads_cursor_and_keeps_original_citation_offsets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAI_BACKEND", "ollama")
    previous = " ".join(f"Ekip {n} numaralı gündem maddesini görüştü." for n in range(20))
    task = "Ayşe sunum dosyasını cuma günü hazırlayacak."
    transcript = previous + " " + task
    prompts: list[str] = []

    def post(*_args: object, **kwargs: object) -> httpx.Response:
        payload = kwargs["json"]
        assert isinstance(payload, dict)
        prompts.append(payload["prompt"])
        # Three recent context sentences, followed by the new assignment.
        return httpx.Response(
            200,
            json={
                "response": json.dumps(
                    {
                        "summary_sentences": [4],
                        "decision_sentences": [],
                        "action_item_sentences": [
                            {"sentence": 4, "owner": "Ayşe", "due_date": "cuma günü"}
                        ],
                    }
                )
            },
            request=httpx.Request("POST", "http://localhost/api/generate"),
        )

    monkeypatch.setattr(httpx, "post", post)
    with TestClient(app) as client:
        response = client.post(
            "/analyze/live",
            json={
                "transcript": transcript,
                "segment_seq": 2,
                "previous_live_cursor": cursor(previous, []).model_dump(),
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert body["version"] == 2 and body["is_partial"] is True
    assert "Ekip 0" not in prompts[0]
    assert body["action_items"] == [{"text": task, "owner": "Ayşe", "due_date": "cuma günü"}]
    citation = body["citations"][0]
    assert citation["source_index"] == 20
    assert citation["source_char_start"] == len(previous) + 1
    assert body["live_cursor"]["active_indices"] == [20]
