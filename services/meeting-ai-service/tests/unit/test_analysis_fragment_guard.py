"""A source quotation can still be an incomplete standalone analysis claim."""

from __future__ import annotations

import json

import httpx
import pytest

from app.core.config import Settings
from app.models.schemas import ActionItem
from app.services.analyze import AnalysisDraft, MeetingAnalysisService
from app.services.citation import ground_claim, split_sentences


class CandidateAnalyzer:
    """Drive the final guard independently of probabilistic model selection."""

    def __init__(self, claim: str) -> None:
        self.claim = claim

    def analyze(self, transcript: str) -> AnalysisDraft:
        return AnalysisDraft(
            summary=self.claim,
            decisions=[self.claim],
            action_items=[ActionItem(text=self.claim)],
        )

    @property
    def model_loaded(self) -> bool:
        return True


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize(
    "fragment",
    [
        "11 olacak.",
        "17 olacak .",
        "10:30 olacak.",
        "11 olsun.",
        "Saat 11 olacak.",
        "11 olabilir.",
        "11 olmasın.",
        "11 olmayacak.",
        "10 değil.",
    ],
)
def test_numeric_continuation_is_withheld_from_all_analysis_categories(
    fragment: str,
    live: bool,
) -> None:
    source = "Zeynep'in sunum dosyasını teslim saati 10 değil. " + fragment
    result = MeetingAnalysisService(Settings(), CandidateAnalyzer(fragment)).analyze(
        source, live=live
    )
    assert result.summary == "" and result.summary_grounding_status == "withheld"
    assert result.decisions == [] and result.action_items == []
    assert {claim.kind for claim in result.rejected_claims} == {"summary", "decision", "action"}
    assert all(
        claim.reason == "context_dependent_numeric_fragment" for claim in result.rejected_claims
    )
    assert all(claim.status == "LOW_CONFIDENCE" for claim in result.rejected_claims)
    assert result.ungrounded_count == 2
    if live:
        assert result.live_cursor is not None and result.live_cursor.active_indices == []


@pytest.mark.parametrize(
    "sentence",
    [
        "11 rapor hazırlanacak.",
        "Rapor teslim saati 11 olacak.",
        "Zeynep raporu saat 11'e kadar hazırlayacak.",
        "Bütçe tablosunu ben kontrol edeceğim.",
        "Mehmet sunum dosyasını hazırlayacak.",
    ],
)
def test_guard_does_not_remove_a_concrete_subject_or_work(sentence: str) -> None:
    result = MeetingAnalysisService(Settings(), CandidateAnalyzer(sentence)).analyze(sentence)
    assert result.summary == sentence
    assert result.decisions == [sentence]
    assert [action.text for action in result.action_items] == [sentence]
    assert result.rejected_claims == []


def test_source_and_general_citation_short_answers_are_not_rewritten() -> None:
    source = "Zeynep'in sunum dosyasını teslim saati 10 değil. 11 olacak."
    sentences = split_sentences(source)
    assert sentences[-1].text == "11 olacak."
    assert source[sentences[-1].start_char : sentences[-1].end_char] == "11 olacak."
    # Short question answers use the general citation guard, not analysis completeness.
    assert ground_claim("11 olacak.", sentences).grounded


def test_wrong_year_in_live_analysis_is_withheld_instead_of_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model must not publish 2020 when the cited source says 2026."""

    wrong_year = "Sunumu 28 Eylül 2020 günü saat 14'te çevrimiçi yapmaya karar verdik."

    def post(*args: object, **kwargs: object) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "response": json.dumps(
                    {
                        "summary": wrong_year,
                        "decisions": [wrong_year],
                        "action_items": [],
                    }
                )
            },
            request=httpx.Request("POST", "http://localhost/api/generate"),
        )

    monkeypatch.setattr(httpx, "post", post)
    source = "Sunumu 28 Eylül 2026 günü saat 14'te çevrimiçi yapmaya karar verdik."
    result = MeetingAnalysisService(Settings(backend="ollama")).analyze(source, live=True)

    assert result.summary == ""
    assert result.decisions == []
    assert result.action_items == []
    assert any(
        claim.reason == "number/quantity in claim not found in source"
        for claim in result.rejected_claims
    )


def test_model_keeps_change_context_but_cannot_publish_orphan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts = []

    def post(*args: object, **kwargs: object) -> httpx.Response:
        payload = kwargs["json"]
        assert isinstance(payload, dict)
        prompts.append(payload["prompt"])
        return httpx.Response(
            200,
            json={
                "response": json.dumps(
                    {
                        "summary_sentences": [3],
                        "decision_sentences": [3],
                        "action_item_sentences": [
                            {"sentence": 3, "owner": None, "due_date": "11 olacak"}
                        ],
                    }
                )
            },
            request=httpx.Request("POST", "http://localhost/api/generate"),
        )

    monkeypatch.setattr(httpx, "post", post)
    source = (
        "Zeynep sunum dosyasını hazırlayacak. "
        "Zeynep'in sunum dosyasını teslim saati 10 değil. 11 olacak."
    )
    result = MeetingAnalysisService(Settings(backend="ollama")).analyze(source, live=True)
    assert "10 değil." in prompts[0] and "11 olacak." in prompts[0]
    assert not result.summary and not result.decisions and not result.action_items
    assert len(result.rejected_claims) == 3
    # No invented repaired task/owner/hour is emitted by this narrow guard.
    assert result.citations == []


def test_legacy_model_output_keeps_valid_neighbors_and_records_orphan_rejections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = "Zeynep sunum dosyasını hazırlayacak."
    decision = "Sunumu çevrim içi yapmaya karar verdik."
    fragment = "11 olacak."

    def post(*args: object, **kwargs: object) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "response": json.dumps(
                    {
                        "summary": task + " " + fragment,
                        "decisions": [decision, fragment],
                        "action_items": [
                            {"text": task, "owner": "Zeynep", "due_date": None},
                            {"text": fragment, "owner": "Zeynep", "due_date": "11 olacak"},
                        ],
                    }
                )
            },
            request=httpx.Request("POST", "http://localhost/api/generate"),
        )

    monkeypatch.setattr(httpx, "post", post)
    source = f"{task} {decision} Zeynep'in teslim saati 10 değil. {fragment}"
    result = MeetingAnalysisService(Settings(backend="ollama")).analyze(source)
    assert result.summary == task and result.summary_grounding_status == "partial_verified"
    assert result.decisions == [decision]
    assert result.action_items == [ActionItem(text=task, owner="Zeynep")]
    assert {claim.kind for claim in result.rejected_claims} == {"summary", "decision", "action"}
    assert len(result.citations) == 2
    assert all(citation.claim != fragment for citation in result.citations)


def test_missing_fragment_source_retains_original_grounding_failure() -> None:
    result = MeetingAnalysisService(Settings(), CandidateAnalyzer("11 olacak.")).analyze(
        "Ekip toplantı salonunu hazırlayacak."
    )
    assert not result.action_items
    assert all(claim.status == "FAILED" for claim in result.rejected_claims)
    assert all(
        claim.reason != "context_dependent_numeric_fragment" for claim in result.rejected_claims
    )
