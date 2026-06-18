"""eval_metrics.py tests — lexical grounding + claim precision/recall (no LLM)."""

from __future__ import annotations

from app.services.eval_metrics import claim_metrics, grounding_rate

TRANSCRIPT = (
    "Bütçe artışı onaylandı. Ali raporu hazırlayacak. Toplantı pazartesi yapılacak."
)


def test_claim_metrics_perfect_match() -> None:
    m = claim_metrics(["Ali raporu hazırlayacak"], ["Ali raporu hazırlayacak"])
    assert m == {"precision": 1.0, "recall": 1.0, "f1": 1.0}


def test_claim_metrics_partial() -> None:
    # 1 of 2 predicted matches → precision 0.5; the matched one covers 1 of 1
    # relevant expected → recall 1.0
    m = claim_metrics(
        expected=["Ali raporu hazırlayacak"],
        predicted=["Ali raporu hazırlayacak", "Ofis taşınacak"],
    )
    assert m["precision"] == 0.5
    assert m["recall"] == 1.0


def test_claim_metrics_duplicate_prediction_not_double_counted() -> None:
    # one-to-one: emitting the same action twice must NOT give precision 1.0
    m = claim_metrics(
        expected=["Ali raporu hazırlayacak"],
        predicted=["Ali raporu hazırlayacak", "Ali raporu hazırlayacak"],
    )
    assert m["precision"] == 0.5  # 1 true positive out of 2 predictions
    assert m["recall"] == 1.0


def test_claim_metrics_empty_sets() -> None:
    assert claim_metrics([], []) == {"precision": 1.0, "recall": 1.0, "f1": 1.0}
    assert claim_metrics(["x görevi"], [])["recall"] == 0.0


def test_grounding_rate_all_grounded() -> None:
    assert grounding_rate(["Bütçe artışı onaylandı", "Toplantı pazartesi"], TRANSCRIPT) == 1.0


def test_grounding_rate_penalizes_fabrication() -> None:
    # 1 of 2 grounded → 0.5
    score = grounding_rate(["Bütçe artışı onaylandı", "Genel müdür istifa etti"], TRANSCRIPT)
    assert score == 0.5


def test_grounding_rate_empty_is_one() -> None:
    assert grounding_rate([], TRANSCRIPT) == 1.0
