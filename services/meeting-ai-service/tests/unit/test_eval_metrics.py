"""eval_metrics.py tests — faithfulness + action precision/recall (no LLM)."""

from __future__ import annotations

from app.services.eval_metrics import action_metrics, faithfulness

TRANSCRIPT = (
    "Bütçe artışı onaylandı. Ali raporu hazırlayacak. Toplantı pazartesi yapılacak."
)


def test_action_metrics_perfect_match() -> None:
    m = action_metrics(["Ali raporu hazırlayacak"], ["Ali raporu hazırlayacak"])
    assert m == {"precision": 1.0, "recall": 1.0, "f1": 1.0}


def test_action_metrics_partial() -> None:
    # 1 of 2 predicted matches → precision 0.5; the matched one covers 1 of 1
    # relevant expected → recall 1.0
    m = action_metrics(
        expected=["Ali raporu hazırlayacak"],
        predicted=["Ali raporu hazırlayacak", "Ofis taşınacak"],
    )
    assert m["precision"] == 0.5
    assert m["recall"] == 1.0


def test_action_metrics_empty_sets() -> None:
    assert action_metrics([], []) == {"precision": 1.0, "recall": 1.0, "f1": 1.0}
    assert action_metrics(["x görevi"], [])["recall"] == 0.0


def test_faithfulness_all_grounded() -> None:
    assert faithfulness(["Bütçe artışı onaylandı", "Toplantı pazartesi"], TRANSCRIPT) == 1.0


def test_faithfulness_penalizes_hallucination() -> None:
    # 1 of 2 grounded → 0.5
    score = faithfulness(["Bütçe artışı onaylandı", "Genel müdür istifa etti"], TRANSCRIPT)
    assert score == 0.5


def test_faithfulness_empty_is_one() -> None:
    assert faithfulness([], TRANSCRIPT) == 1.0
