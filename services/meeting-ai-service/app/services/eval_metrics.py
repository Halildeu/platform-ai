"""#162 G-INT gate metrics — faithfulness + action precision/recall.

The intelligence-quality counterpart of live-stt's WER and diarization's DER:
turn an analyzer's output into measurable numbers against a small ground-truth
eval set. Pure-Python, deterministic, CPU-unit-testable; the heavy LLM step
(Ollama) only produces the output being scored (scripts/intel_eval.py).

- **faithfulness** = fraction of decisions/actions grounded in the transcript
  (reuses citation grounding — the hallucination guard, as a 0..1 score).
- **action precision/recall/F1** = predicted action items vs an expected set,
  matched by normalized token overlap (same metric family as citation).
"""

from __future__ import annotations

from app.services.citation import _similarity, _tokens, ground_claims


def _claims_match(a: str, b: str, threshold: float) -> bool:
    """Two claims match if their normalized token overlap clears the threshold."""
    return _similarity(_tokens(a), _tokens(b)) >= threshold


def action_metrics(
    expected: list[str], predicted: list[str], threshold: float = 0.5
) -> dict[str, float]:
    """Precision/recall/F1 of predicted action items vs an expected set."""
    exp = [e for e in expected if e.strip()]
    pred = [p for p in predicted if p.strip()]
    if not exp and not pred:
        return {"precision": 1.0, "recall": 1.0, "f1": 1.0}
    tp_pred = sum(1 for p in pred if any(_claims_match(p, e, threshold) for e in exp))
    tp_exp = sum(1 for e in exp if any(_claims_match(e, p, threshold) for p in pred))
    precision = tp_pred / len(pred) if pred else 0.0
    recall = tp_exp / len(exp) if exp else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {"precision": round(precision, 3), "recall": round(recall, 3), "f1": round(f1, 3)}


def faithfulness(claims: list[str], transcript: str) -> float:
    """Fraction of claims grounded in the transcript (1.0 = fully faithful)."""
    total = len([c for c in claims if c.strip()])
    if total == 0:
        return 1.0
    _, ungrounded = ground_claims(claims, transcript)
    return round(1.0 - ungrounded / total, 3)
