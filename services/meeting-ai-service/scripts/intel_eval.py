"""#162 G-INT eval runner — score an analyzer over a ground-truth eval set.

The intelligence sibling of wer_matrix.py / diar_matrix.py: feed transcripts +
expected decisions/actions, run MeetingAnalysisService, and report a lexical
grounding rate + decision/action precision/recall/F1 — one JSON row that is the
reproducible evidence artifact (carries backend+model+eval_set). Mock backend
runs on CPU (CI); the `ollama` backend needs the GPU host (real LLM).

Metrics are LEXICAL (token overlap), not semantic — see eval_metrics.py. The
runner scores analyze() only; ask-AI is not gated here (ADR-0034 scope).

Eval set (JSON):
  {"samples": [
     {"transcript": "...", "expected_decisions": ["..."], "expected_actions": ["..."]}
  ]}

Usage:
  # CPU (mock): tooling smoke
  MAI_BACKEND=mock python scripts/intel_eval.py --eval-set tests/fixtures/intel-eval.json
  # GPU host (real LLM):
  MAI_BACKEND=ollama python scripts/intel_eval.py --eval-set <set>.json --tag ollama-8b
"""
# ruff: noqa: T201 - measurement CLI: prints are the output.

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # service root → app importable

from app.core.config import get_settings
from app.services.analyze import MeetingAnalysisService
from app.services.eval_metrics import claim_metrics, grounding_rate


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--eval-set", required=True, help="JSON with samples[]")
    ap.add_argument("--tag", default="", help="label for the JSON result row")
    ap.add_argument("--match-threshold", type=float, default=0.5)
    args = ap.parse_args()

    data = json.loads(Path(args.eval_set).read_text(encoding="utf-8"))
    samples = data.get("samples", [])
    if not samples:
        print(f"NO samples in {args.eval_set}", file=sys.stderr)
        sys.exit(2)

    settings = get_settings()
    svc = MeetingAnalysisService(settings)

    groundings: list[float] = []
    act_p: list[float] = []
    act_r: list[float] = []
    act_f: list[float] = []
    dec_p: list[float] = []
    dec_r: list[float] = []
    dec_f: list[float] = []
    latencies: list[float] = []
    for i, s in enumerate(samples, 1):
        transcript = str(s.get("transcript", ""))
        t0 = time.perf_counter()
        r = svc.analyze(transcript)
        latencies.append((time.perf_counter() - t0) * 1000)
        claims = list(r.decisions) + [a.text for a in r.action_items]
        groundings.append(grounding_rate(claims, transcript))
        am = claim_metrics(
            list(s.get("expected_actions", [])),
            [a.text for a in r.action_items],
            args.match_threshold,
        )
        dm = claim_metrics(
            list(s.get("expected_decisions", [])),
            list(r.decisions),
            args.match_threshold,
        )
        act_p.append(am["precision"])
        act_r.append(am["recall"])
        act_f.append(am["f1"])
        dec_p.append(dm["precision"])
        dec_r.append(dm["recall"])
        dec_f.append(dm["f1"])
        print(
            f"  [{i}/{len(samples)}] grounding={groundings[-1]} "
            f"act P={am['precision']} R={am['recall']} "
            f"dec P={dm['precision']} R={dm['recall']}",
            file=sys.stderr,
        )

    def _mean(xs: list[float]) -> float:
        return round(statistics.mean(xs), 3)

    row = {
        "tag": args.tag or settings.backend,
        "backend": settings.backend,
        "model": svc.effective_model,
        "eval_set": args.eval_set,
        "n_samples": len(samples),
        "grounding_rate": _mean(groundings),  # lexical, not semantic faithfulness
        "action_precision": _mean(act_p),
        "action_recall": _mean(act_r),
        "action_f1": _mean(act_f),  # macro: per-sample F1 mean (not F1-of-means)
        "decision_precision": _mean(dec_p),
        "decision_recall": _mean(dec_r),
        "decision_f1": _mean(dec_f),
        "p50_ms": round(statistics.median(latencies)),
        "metric_note": "lexical token-overlap; macro per-sample means",
    }
    print(json.dumps(row, ensure_ascii=False))
    print(
        f"== {row['tag']} ({row['model']}): grounding {row['grounding_rate']:.0%}, "
        f"action P {row['action_precision']:.0%}/R {row['action_recall']:.0%}, "
        f"decision P {row['decision_precision']:.0%}/R {row['decision_recall']:.0%} "
        f"on {row['n_samples']} samples",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
