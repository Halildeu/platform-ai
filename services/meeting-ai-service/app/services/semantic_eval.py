"""Exact sentence-label evaluation, separate from citation/lexical grounding.

This measures agreement with versioned synthetic annotations, not general human
meeting accuracy. A source sentence can be perfectly grounded and mislabeled.
Duplicates consume one gold label only; missing/error outputs cannot pass gates.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.schemas import ActionItem, AnalyzeResponse
from app.services.citation import split_sentences


class GoldAction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    sentence: int = Field(ge=1)
    owner: str | None = None
    due_date: str | None = None


class GoldCase(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(pattern=r"^[a-z0-9-]+$")
    sentences: list[str] = Field(min_length=1)
    decisions: list[int]
    actions: list[GoldAction]
    rationale: str = Field(min_length=1)

    @property
    def transcript(self) -> str:
        return " ".join(self.sentences)

    @model_validator(mode="after")
    def validate_labels(self) -> GoldCase:
        actual = [sentence.text for sentence in split_sentences(self.transcript)]
        if actual != self.sentences or len(set(actual)) != len(actual):
            raise ValueError("gold sentences must be unique canonical source sentences")
        indices = self.decisions + [action.sentence for action in self.actions]
        if any(index < 1 or index > len(actual) for index in indices):
            raise ValueError("gold label outside source sentence range")
        if len(set(self.decisions)) != len(self.decisions) or len(
            {action.sentence for action in self.actions}
        ) != len(self.actions):
            raise ValueError("duplicate gold labels")
        for action in self.actions:
            source = actual[action.sentence - 1]
            if any(
                value is not None and value not in source
                for value in (action.owner, action.due_date)
            ):
                raise ValueError("gold metadata must occur in the same source sentence")
        return self


class GoldCorpus(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["meeting-semantic-gold-v1"]
    synthetic: Literal[True]
    definitions: dict[str, str]
    cases: list[GoldCase] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_cases(self) -> GoldCorpus:
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("duplicate case identifiers")
        return self


def load_corpus(path: Path) -> GoldCorpus:
    return GoldCorpus.model_validate(json.loads(path.read_text(encoding="utf-8")))


def exact_counts(expected: list[str], predicted: list[str]) -> dict[str, int | float]:
    remaining = Counter(expected)
    true_positive = 0
    for claim in predicted:
        if remaining[claim]:
            true_positive += 1
            remaining[claim] -= 1
    return counts_metrics(true_positive, len(predicted) - true_positive, sum(remaining.values()))


def counts_metrics(tp: int, fp: int, fn: int) -> dict[str, int | float]:
    precision = tp / (tp + fp) if tp + fp else float(fn == 0)
    recall = tp / (tp + fn) if tp + fn else float(fp == 0)
    return {
        "true_positive": tp,
        "false_positive": fp,
        "false_negative": fn,
        "precision": precision,
        "recall": recall,
        "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
    }


def _action_key(action: ActionItem) -> str:
    return json.dumps([action.text, action.owner, action.due_date], ensure_ascii=False)


def score_case(case: GoldCase, result: AnalyzeResponse | None) -> dict[str, object]:
    """Score shipped service output; never emit source text or generated claims."""
    expected_decisions = [case.sentences[index - 1] for index in case.decisions]
    expected_actions = [
        ActionItem(
            text=case.sentences[action.sentence - 1], owner=action.owner, due_date=action.due_date
        )
        for action in case.actions
    ]
    decisions = result.decisions if result else []
    actions = result.action_items if result else []
    claims = decisions + [action.text for action in actions]
    grounded = sum(claim in case.sentences for claim in claims)
    decision_score = exact_counts(expected_decisions, decisions)
    action_score = exact_counts(
        [action.text for action in expected_actions], [action.text for action in actions]
    )
    joint_score = exact_counts(
        [_action_key(action) for action in expected_actions],
        [_action_key(action) for action in actions],
    )
    metadata: dict[str, object] = {}
    for field in ("owner", "due_date"):
        expected = {action.text: getattr(action, field) for action in expected_actions}
        remaining = dict(expected)
        correct = 0
        for action in actions:
            if action.text in remaining:
                correct += getattr(action, field) == remaining.pop(action.text)
        denominator = max(len(expected_actions), len(actions))
        metadata[field] = {
            "exact_match": correct,
            "evaluated": denominator,
            "accuracy": correct / denominator if denominator else 1.0,
        }
    return {
        "id": case.id,
        "error": result is None,
        "decision": decision_score,
        "action": action_score,
        "action_with_metadata": joint_score,
        "metadata": metadata,
        "grounding": {
            "exact_source_count": grounded,
            "claim_count": len(claims),
            "rate": grounded / len(claims) if claims else 1.0,
        },
        "exact_case_match": result is not None
        and all(
            score["false_positive"] == score["false_negative"] == 0
            for score in (decision_score, joint_score)
        ),
    }


def aggregate_scores(rows: list[dict[str, object]]) -> dict[str, object]:
    """Micro-averages avoid inflating results with many empty negative cases."""
    output: dict[str, object] = {
        "case_count": len(rows),
        "error_count": sum(row["error"] is True for row in rows),
        "exact_case_count": sum(row["exact_case_match"] is True for row in rows),
    }
    for kind in ("decision", "action", "action_with_metadata"):
        scores = [row[kind] for row in rows]
        counts = [
            sum(int(score[key]) for score in scores if isinstance(score, dict))
            for key in ("true_positive", "false_positive", "false_negative")
        ]
        output[kind] = counts_metrics(*counts)
    output["all_cases_exact"] = bool(rows) and output["exact_case_count"] == len(rows)
    return output
