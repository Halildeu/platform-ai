"""Observed synthetic ASR regression; unit tests do not measure model quality."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from app.core.config import Settings
from app.models.schemas import ActionItem, AnalyzeResponse
from app.services.analyze import MeetingAnalysisService
from app.services.citation import split_sentences
from app.services.semantic_eval import load_corpus, score_case

FIXTURES = Path(__file__).parents[1] / "fixtures"
CORPUS = FIXTURES / "meeting-semantic-runtime-asr-v1.json"
ROWS = FIXTURES / "meeting-semantic-runtime-asr-rows-v1.json"


def test_observed_asr_annotations_and_rows_are_frozen() -> None:
    assert hashlib.sha256(CORPUS.read_bytes()).hexdigest() == (
        "7ddd9d4346e7943df8c8c7e30b6d7af1fa4039403d27466a3f08409950af0097"
    )
    assert hashlib.sha256(ROWS.read_bytes()).hexdigest() == (
        "7909d41032351191c01e41010a236d082246fe6ef63989c31f5ea0453b7ab19c"
    )
    original = load_corpus(FIXTURES / "meeting-semantic-gold-v1.json")
    observed = load_corpus(CORPUS)
    assert len(observed.cases) == 4
    assert not {case.id for case in original.cases} & {case.id for case in observed.cases}
    case = observed.cases[0]
    assert len(case.sentences) == 19
    assert case.decisions == [12, 13, 17, 18]
    assert [action.sentence for action in case.actions] == [10, 14, 15]
    assert [action.owner for action in case.actions] == [None, None, None]
    assert [action.due_date for action in case.actions] == [None, None, "Perşembe günü"]
    assert observed.cases[1].decisions == [3, 4]
    assert observed.cases[2].decisions == observed.cases[2].actions == []
    assert observed.cases[3].decisions == observed.cases[3].actions == []


def test_actual_rows_preserve_exact_source_and_policy_boundaries() -> None:
    source = json.loads(ROWS.read_text())
    assert source["synthetic"] is True
    assert len(source["rows"]) == 112
    transcript = "\n".join(source["rows"])
    assert hashlib.sha256(transcript.encode()).hexdigest() == (
        "b793cffc60d0cc3b56324d0cf82a28459e2d18c158e19476ac653ae175cd01b3"
    )
    sentences = split_sentences(transcript)
    assert [sentence.text for sentence in sentences] == load_corpus(CORPUS).cases[0].sentences
    for sentence in sentences:
        raw_span = transcript[sentence.start_char : sentence.end_char]
        assert "".join(raw_span.split()) == "".join(sentence.text.split())


@pytest.mark.parametrize("observed_rows", [False, True])
def test_atomic_selection_keeps_metadata_and_grounding_without_rewriting(
    monkeypatch: pytest.MonkeyPatch, observed_rows: bool
) -> None:
    case = load_corpus(CORPUS).cases[0]
    transcript = case.transcript
    if observed_rows:
        transcript = "\n".join(json.loads(ROWS.read_text())["rows"])
    captured: dict[str, object] = {}
    selection = {
        "summary_sentences": [12, 17, 18],
        "decision_sentences": case.decisions,
        "action_item_sentences": [action.model_dump() for action in case.actions],
    }

    def respond(*args: object, **kwargs: object) -> httpx.Response:
        captured.update(kwargs["json"])  # type: ignore[arg-type]
        return httpx.Response(
            200,
            json={"response": json.dumps(selection)},
            request=httpx.Request("POST", "http://localhost:11434/api/generate"),
        )

    monkeypatch.setattr(httpx, "post", respond)
    result = MeetingAnalysisService(Settings(backend="ollama")).analyze(transcript)
    assert result.decisions == [case.sentences[index - 1] for index in [12, 13, 17, 18]]
    assert result.action_items[-1].due_date == "Perşembe günü"
    score = score_case(case, result)
    assert score["exact_case_match"] is True
    prompt = str(captured["prompt"])
    assert "[16] Kararımız açık." in prompt
    assert "[17] Önce test ortamında doğrulama yapılacak." in prompt
    assert "[18] Ardından yayın kararı verilecek." in prompt


def test_observed_missing_policy_and_due_date_still_fail_semantic_gate() -> None:
    case = load_corpus(CORPUS).cases[0]
    observed = AnalyzeResponse(
        summary="",
        decisions=[case.sentences[index - 1] for index in [12, 13]],
        action_items=[
            ActionItem(text=case.sentences[index - 1], owner=None, due_date=None)
            for index in [10, 14, 15]
        ],
        redacted=True,
        redaction_count=0,
        backend="ollama",
        model="observed-failure-replay-not-model-invocation",
        elapsed_ms=0,
    )
    score = score_case(case, observed)
    assert score["exact_case_match"] is False
    assert score["decision"]["true_positive"] == 2
    assert score["decision"]["false_negative"] == 2
    assert score["action"]["true_positive"] == 3
    assert score["action_with_metadata"]["true_positive"] == 2
    assert score["action_with_metadata"]["false_positive"] == 1
    assert score["action_with_metadata"]["false_negative"] == 1
    assert score["grounding"]["rate"] == 1.0
