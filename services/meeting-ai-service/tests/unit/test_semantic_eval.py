"""Exact labels must not confuse source copying with decision correctness."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.models.schemas import ActionItem, AnalyzeResponse
from app.services.semantic_eval import (
    GoldCase,
    GoldCorpus,
    aggregate_scores,
    exact_counts,
    load_corpus,
    score_case,
)

FIXTURE = Path(__file__).parents[1] / "fixtures" / "meeting-semantic-gold-v1.json"


def response(decisions: list[str], actions: list[ActionItem] | None = None) -> AnalyzeResponse:
    return AnalyzeResponse(
        summary="",
        decisions=decisions,
        action_items=actions or [],
        redacted=True,
        redaction_count=0,
        backend="ollama",
        model="test",
        elapsed_ms=0,
    )


def test_high_grounding_is_not_semantic_precision() -> None:
    case = load_corpus(FIXTURE).cases[0]
    score = score_case(case, response([case.sentences[0]]))
    assert score["grounding"] == {"exact_source_count": 1, "claim_count": 1, "rate": 1.0}
    assert score["decision"]["false_positive"] == 1
    assert score["decision"]["precision"] == 0.0
    assert score["exact_case_match"] is False


def test_duplicate_predictions_are_false_positives() -> None:
    score = exact_counts(["Onaylandı."], ["Onaylandı.", "Onaylandı."])
    assert score["true_positive"] == 1
    assert score["false_positive"] == 1
    assert score["precision"] == 0.5


def test_similar_tokens_do_not_match_another_sentence_label() -> None:
    score = exact_counts(["Bütçe değişmedi."], ["Bütçe değişti."])
    assert score["true_positive"] == 0
    assert score["false_positive"] == score["false_negative"] == 1


@pytest.mark.parametrize(
    "expected,predicted,precision,recall",
    [
        ([], [], 1.0, 1.0),
        (["x"], [], 0.0, 0.0),
        ([], ["x"], 0.0, 0.0),
    ],
)
def test_empty_label_edges(
    expected: list[str], predicted: list[str], precision: float, recall: float
) -> None:
    score = exact_counts(expected, predicted)
    assert score["precision"] == precision
    assert score["recall"] == recall


def test_metadata_errors_are_distinct_from_correct_action_text() -> None:
    case = load_corpus(FIXTURE).cases[4]
    score = score_case(
        case,
        response([], [ActionItem(text=case.sentences[0], owner="Speaker 1", due_date="yarın")]),
    )
    assert score["action"]["precision"] == 1.0
    assert score["action_with_metadata"]["precision"] == 0.0
    assert score["metadata"]["owner"]["accuracy"] == 0.0
    assert score["metadata"]["due_date"]["accuracy"] == 0.0


def test_missing_output_is_error_even_with_empty_gold() -> None:
    score = score_case(load_corpus(FIXTURE).cases[0], None)
    assert score["error"] is True
    assert score["exact_case_match"] is False
    assert aggregate_scores([score])["all_cases_exact"] is False
    assert aggregate_scores([score])["error_count"] == 1


def test_gold_oracle_matches_all_cases_without_text_in_report() -> None:
    corpus = load_corpus(FIXTURE)
    rows = []
    for case in corpus.cases:
        result = response(
            [case.sentences[index - 1] for index in case.decisions],
            [
                ActionItem(
                    text=case.sentences[action.sentence - 1],
                    owner=action.owner,
                    due_date=action.due_date,
                )
                for action in case.actions
            ],
        )
        rows.append(score_case(case, result))
    assert len(corpus.cases) == 12
    aggregate = aggregate_scores(rows)
    assert aggregate["all_cases_exact"] is True
    assert aggregate["error_count"] == 0
    serialized = json.dumps(rows, ensure_ascii=False)
    assert all(sentence not in serialized for case in corpus.cases for sentence in case.sentences)


@pytest.mark.parametrize(
    "change",
    [
        {"decisions": [999]},
        {"decisions": [True]},
        {"decisions": [1, 1]},
        {"sentences": ["Bir cümle. İki cümle."]},
        {"actions": [{"sentence": 1, "owner": "Uydurma sahip"}]},
    ],
)
def test_invalid_annotations_fail_closed(change: dict[str, object]) -> None:
    data = load_corpus(FIXTURE).cases[0].model_dump()
    data.update(change)
    with pytest.raises(ValidationError):
        GoldCase.model_validate(data)


def test_duplicate_case_and_nonsynthetic_corpus_rejected() -> None:
    data = load_corpus(FIXTURE).model_dump()
    data["cases"].append(data["cases"][0])
    with pytest.raises(ValidationError):
        GoldCorpus.model_validate(data)
    data = load_corpus(FIXTURE).model_dump()
    data["synthetic"] = False
    with pytest.raises(ValidationError):
        GoldCorpus.model_validate(data)


def test_empty_aggregate_cannot_pass() -> None:
    assert aggregate_scores([])["all_cases_exact"] is False
