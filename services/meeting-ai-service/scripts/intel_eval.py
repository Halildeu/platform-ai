"""#162 G-INT eval runner — score an analyzer over a ground-truth eval set.

The intelligence sibling of wer_matrix.py / diar_matrix.py: feed transcripts +
expected decisions/actions, run MeetingAnalysisService, and report faithfulness
+ action precision/recall/F1 — one JSON row. Mock backend runs on CPU (CI);
the `ollama` backend needs the GPU host (real LLM).

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
from app.services.eval_metrics import action_metrics, faithfulness


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

    faiths: list[float] = []
    precisions: list[float] = []
    recalls: list[float] = []
    f1s: list[float] = []
    latencies: list[float] = []
    for i, s in enumerate(samples, 1):
        transcript = str(s.get("transcript", ""))
        t0 = time.perf_counter()
        r = svc.analyze(transcript)
        latencies.append((time.perf_counter() - t0) * 1000)
        claims = list(r.decisions) + [a.text for a in r.action_items]
        faiths.append(faithfulness(claims, transcript))
        am = action_metrics(
            list(s.get("expected_actions", [])),
            [a.text for a in r.action_items],
            args.match_threshold,
        )
        precisions.append(am["precision"])
        recalls.append(am["recall"])
        f1s.append(am["f1"])
        print(f"  [{i}/{len(samples)}] faith={faiths[-1]} P={am['precision']} R={am['recall']}",
              file=sys.stderr)

    row = {
        "tag": args.tag or settings.backend,
        "backend": settings.backend,
        "n_samples": len(samples),
        "faithfulness": round(statistics.mean(faiths), 3),
        "action_precision": round(statistics.mean(precisions), 3),
        "action_recall": round(statistics.mean(recalls), 3),
        "action_f1": round(statistics.mean(f1s), 3),
        "p50_ms": round(statistics.median(latencies)),
    }
    print(json.dumps(row, ensure_ascii=False))
    print(
        f"== {row['tag']}: faith {row['faithfulness']:.0%}, "
        f"action P {row['action_precision']:.0%}/R {row['action_recall']:.0%} "
        f"(F1 {row['action_f1']:.0%}) on {row['n_samples']} samples",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
