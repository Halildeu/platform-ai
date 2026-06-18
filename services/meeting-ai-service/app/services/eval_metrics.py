"""#162 G-INT gate metrics — lexical grounding + claim precision/recall.

The intelligence-quality counterpart of live-stt's WER and diarization's DER:
turn an analyzer's output into measurable numbers against a small ground-truth
eval set. Pure-Python, deterministic, CPU-unit-testable; the heavy LLM step
(Ollama) only produces the output being scored (scripts/intel_eval.py).

**Honest naming (review #166):** these are *lexical* metrics built on token
overlap, NOT semantic-truth metrics.

- **grounding_rate** = fraction of decisions/actions whose tokens overlap some
  transcript sentence above threshold. It catches fabrication (claims with no
  lexical support) but does NOT catch a claim that flips meaning while reusing
  words ("onaylandı" → "reddedildi"). It is a hallucination *floor*, not a
  faithfulness guarantee. Real semantic faithfulness needs entailment/NLI — out
  of scope here and called out in ADR-0034.
- **precision/recall/F1** = predicted claims vs an expected set, matched
  one-to-one (greedy) by normalized token overlap. One-to-one matching stops a
  model from inflating precision by emitting the same claim twice.
"""

from __future__ import annotations

from app.services.citation import _similarity, _tokens, ground_claims


def _claims_match(a: str, b: str, threshold: float) -> bool:
    """Two claims match if their normalized token overlap clears the threshold."""
    return _similarity(_tokens(a), _tokens(b)) >= threshold


def claim_metrics(
    expected: list[str], predicted: list[str], threshold: float = 0.5
) -> dict[str, float]:
    """Precision/recall/F1 of predicted claims vs an expected set (one-to-one).

    Each predicted claim is greedily matched to at most one still-unmatched
    expected claim, so duplicate predictions cannot each count as a true
    positive (review: lexical many-to-one inflated precision).
    """
    exp = [e for e in expected if e.strip()]
    pred = [p for p in predicted if p.strip()]
    if not exp and not pred:
        return {"precision": 1.0, "recall": 1.0, "f1": 1.0}

    matched_exp: set[int] = set()
    tp = 0
    for p in pred:
        for j, e in enumerate(exp):
            if j in matched_exp:
                continue
            if _claims_match(p, e, threshold):
                matched_exp.add(j)
                tp += 1
                break  # this prediction consumes exactly one expected

    precision = tp / len(pred) if pred else 0.0
    recall = tp / len(exp) if exp else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {"precision": round(precision, 3), "recall": round(recall, 3), "f1": round(f1, 3)}


def action_metrics(
    expected: list[str], predicted: list[str], threshold: float = 0.5
) -> dict[str, float]:
    """Back-compat alias — actions scored with the same one-to-one matcher."""
    return claim_metrics(expected, predicted, threshold)


def grounding_rate(claims: list[str], transcript: str) -> float:
    """Fraction of claims lexically grounded in the transcript (1.0 = none fabricated).

    NOT semantic faithfulness — see module docstring. A claim that reuses
    transcript words but inverts meaning still counts as grounded.
    """
    total = len([c for c in claims if c.strip()])
    if total == 0:
        return 1.0
    _, ungrounded = ground_claims(claims, transcript)
    return round(1.0 - ungrounded / total, 3)
