"""Frozen independent challenge annotations; no model invocation in these tests."""

from __future__ import annotations

import hashlib
from pathlib import Path

from app.models.schemas import ActionItem, AnalyzeResponse
from app.services.semantic_eval import aggregate_scores, load_corpus, score_case

FIXTURES = Path(__file__).parents[1] / "fixtures"


def test_original_corpus_remains_frozen() -> None:
    assert hashlib.sha256(
        (FIXTURES / "meeting-semantic-gold-v1.json").read_bytes()
    ).hexdigest() == ("6d09b0dc359d99d0598189cf78104021a0852b688c71fb22102e89f33aa7c1f0")


def test_challenge_corpus_has_distinct_cases_and_canonical_gold() -> None:
    original = load_corpus(FIXTURES / "meeting-semantic-gold-v1.json")
    challenge = load_corpus(FIXTURES / "meeting-semantic-challenge-v1.json")
    assert len(challenge.cases) == 6
    assert not {case.id for case in original.cases} & {case.id for case in challenge.cases}
    assert not {case.transcript for case in original.cases} & {
        case.transcript for case in challenge.cases
    }
    rows = []
    for case in challenge.cases:
        result = AnalyzeResponse(
            summary="",
            decisions=[case.sentences[index - 1] for index in case.decisions],
            action_items=[
                ActionItem(
                    text=case.sentences[action.sentence - 1],
                    owner=action.owner,
                    due_date=action.due_date,
                )
                for action in case.actions
            ],
            redacted=True,
            redaction_count=0,
            backend="ollama",
            model="gold-oracle-not-model-output",
            elapsed_ms=0,
        )
        rows.append(score_case(case, result))
    aggregate = aggregate_scores(rows)
    assert aggregate["all_cases_exact"] is True
    assert aggregate["decision"]["true_positive"] == 4
    assert aggregate["action"]["true_positive"] == 9


def test_challenge_annotations_cover_context_and_multilabel_edges() -> None:
    cases = {
        case.id: case for case in load_corpus(FIXTURES / "meeting-semantic-challenge-v1.json").cases
    }
    both = cases["challenge-both-choice-and-assignment"]
    assert both.decisions == [action.sentence for action in both.actions] == [2]
    canceled = cases["challenge-later-canceled-task"]
    assert canceled.decisions == [3]
    assert [action.sentence for action in canceled.actions] == [4]
    historical = cases["challenge-reported-historical-approval"]
    assert historical.decisions == historical.actions == []
    injection = cases["challenge-transcript-prompt-injection"]
    assert "999" in injection.transcript
    assert injection.decisions == [4]
    assert [action.sentence for action in injection.actions] == [5]
    late = cases["challenge-long-distractors-late-outcomes"]
    assert min(late.decisions) > 15
    assert min(action.sentence for action in late.actions) > 15
    concurrent = cases["challenge-concurrent-explicit-owners"]
    assert len({action.owner for action in concurrent.actions}) == 4
    assert len({action.due_date for action in concurrent.actions}) == 4
