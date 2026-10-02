"""Reproduce the existing boundary with recorded synthetic source, no inference."""

import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.core.config import Settings
from app.models.schemas import ActionItem
from app.services.analyze import AnalysisDraft, MeetingAnalysisService, OllamaAnalyzer
from app.services.citation import split_sentences
from app.services.extractive import selectable_sentences

FIXTURE = json.loads((Path(__file__).parents[1] / "fixtures/normal.json").read_text("utf-8"))
TEXT = FIXTURE["transcript"]


def test_fixture_is_server_verified_synthetic_text() -> None:
    assert FIXTURE["synthetic"] is True
    assert hashlib.sha256(TEXT.encode()).hexdigest() == FIXTURE["transcriptSha256"]
    assert " ".join(e["text"].strip() for e in FIXTURE["finalEvents"]) == TEXT
    assert [a["owner"] for a in FIXTURE["observedActions"]] == ["Zeynep", None]


def test_existing_selector_discards_the_detached_name() -> None:
    all_sources = split_sentences(TEXT)
    assert all_sources[3].text == "Mehmet."
    assert all_sources[3] not in selectable_sentences(all_sources)


def test_existing_live_prompt_does_not_receive_mehmet(monkeypatch: pytest.MonkeyPatch) -> None:
    prompts: list[str] = []

    def response(settings: Settings, payload: dict[str, Any], **kwargs: object) -> httpx.Response:
        prompts.append(payload["prompt"])
        return httpx.Response(
            200,
            json={
                "response": json.dumps(
                    {"summary_sentences": [], "decision_sentences": [], "action_item_sentences": []}
                )
            },
        )

    monkeypatch.setattr("app.services.analyze.generate", response)
    OllamaAnalyzer(Settings(backend="ollama")).analyze_live(TEXT, None)
    assert len(prompts) == 1
    assert "Zeynep" in prompts[0]
    assert "Mehmet" not in prompts[0]


@pytest.mark.parametrize("proposed_owner", ["Mehmet", None])
def test_existing_guard_and_null_draft_produce_the_same_visible_gap(
    proposed_owner: str | None,
) -> None:
    class ControlledAnalyzer:
        model_loaded = True

        def analyze(self, transcript: str) -> AnalysisDraft:
            actions = [ActionItem.model_validate(a) for a in FIXTURE["observedActions"]]
            actions[1].owner = proposed_owner
            return AnalysisDraft(action_items=actions)

    result = MeetingAnalysisService(
        Settings(backend="mock", redact_pii=False), analyzer=ControlledAnalyzer()
    ).analyze(TEXT, live=True)
    assert [a.owner for a in result.action_items] == ["Zeynep", None]
    assert [r.kind for r in result.rejected_claims] == (["action_owner"] if proposed_owner else [])
    assert result.citations[1].source_index == 4
    assert result.citations[1].source_text == split_sentences(TEXT)[4].text
