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
import hashlib
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # service root → app importable

from app.core.config import Settings, get_settings
from app.services.analyze import (
    _OLLAMA_PROMPT,
    BackendUnavailableError,
    MeetingAnalysisService,
    OllamaSchemaInvalidError,
    OllamaUnparseableOutputError,
)
from app.services.eval_metrics import claim_metrics, grounding_rate

_METRIC_KEYS = (
    "grounding_rate",
    "action_precision",
    "action_recall",
    "action_f1",
    "decision_precision",
    "decision_recall",
    "decision_f1",
    "schema_invalid_rate",
    "format_invalid_rate",
    "backend_error_rate",
    "truncation_risk_rate",
    "p50_ms",
)

# Coarse Turkish chars→tokens ratio for a truncation pre-check (no tokenizer dep);
# deliberately conservative so the flag fires BEFORE real truncation.
_CHARS_PER_TOKEN = 3.0


def _sha12(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _ollama_fingerprint(settings: Settings) -> dict[str, object]:
    """Best-effort live artifact fingerprint (model digest + Ollama version) so a
    result row is reproducible even though model tags are mutable. Null when the
    Ollama host is unreachable (mock/CI)."""
    fp: dict[str, object] = {"ollama_version": None, "model_digest": None}
    if settings.backend != "ollama":
        return fp
    try:
        fp["ollama_version"] = (
            httpx.get(f"{settings.ollama_host}/api/version", timeout=3).json().get("version")
        )
        show = httpx.post(
            f"{settings.ollama_host}/api/show",
            json={"name": settings.ollama_model},
            timeout=5,
        ).json()
        fp["model_digest"] = (show.get("details") or {}).get("digest") or show.get("digest")
    except (httpx.HTTPError, ValueError, KeyError, AttributeError):
        pass  # unreachable host → leave nulls; the row still records the tag
    return fp


def _score_run(
    svc: MeetingAnalysisService,
    samples: list[dict[str, Any]],
    threshold: float,
    num_ctx: int,
    is_ollama: bool,
) -> dict[str, float]:
    """Score one analyzer over the eval set → mean metrics for a single run.

    A backend that breaks the JSON schema raises ``BackendUnavailableError``; that
    sample is counted as ``schema_invalid`` (zero-credit) instead of crashing the
    whole bakeoff or silently reading as low recall (Codex review). A coarse
    transcript/num_ctx pre-check flags ``truncation_risk`` so a long-meeting recall
    drop is not mistaken for a model weakness.
    """
    groundings: list[float] = []
    act_p: list[float] = []
    act_r: list[float] = []
    act_f: list[float] = []
    dec_p: list[float] = []
    dec_r: list[float] = []
    dec_f: list[float] = []
    latencies: list[float] = []
    schema_invalid = 0
    format_invalid = 0
    backend_error = 0
    truncation_risk = 0

    def _zero_credit() -> None:
        for xs in (groundings, act_p, act_r, act_f, dec_p, dec_r, dec_f):
            xs.append(0.0)

    for i, s in enumerate(samples, 1):
        transcript = str(s.get("transcript", ""))
        if is_ollama and len(transcript) / _CHARS_PER_TOKEN > num_ctx:
            truncation_risk += 1
        t0 = time.perf_counter()
        try:
            r = svc.analyze(transcript)
        except OllamaSchemaInvalidError:
            # parseable JSON, wrong shape → model contract failure
            schema_invalid += 1
            latencies.append((time.perf_counter() - t0) * 1000)
            _zero_credit()
            print(f"    [{i}/{len(samples)}] SCHEMA_INVALID (wrong-shape JSON)", file=sys.stderr)
            continue
        except OllamaUnparseableOutputError:
            # not even valid JSON → also a model output-contract failure, NOT infra
            format_invalid += 1
            latencies.append((time.perf_counter() - t0) * 1000)
            _zero_credit()
            print(f"    [{i}/{len(samples)}] FORMAT_INVALID (unparseable output)", file=sys.stderr)
            continue
        except BackendUnavailableError:
            # infra only: host down / HTTP / timeout — NOT a model-quality signal
            backend_error += 1
            latencies.append((time.perf_counter() - t0) * 1000)
            _zero_credit()
            print(f"    [{i}/{len(samples)}] BACKEND_ERROR (infra, not model)", file=sys.stderr)
            continue
        latencies.append((time.perf_counter() - t0) * 1000)
        claims = list(r.decisions) + [a.text for a in r.action_items]
        groundings.append(grounding_rate(claims, transcript))
        am = claim_metrics(
            list(s.get("expected_actions", [])),
            [a.text for a in r.action_items],
            threshold,
        )
        dm = claim_metrics(
            list(s.get("expected_decisions", [])),
            list(r.decisions),
            threshold,
        )
        act_p.append(am["precision"])
        act_r.append(am["recall"])
        act_f.append(am["f1"])
        dec_p.append(dm["precision"])
        dec_r.append(dm["recall"])
        dec_f.append(dm["f1"])
        print(
            f"    [{i}/{len(samples)}] grounding={groundings[-1]} "
            f"act P={am['precision']} R={am['recall']} "
            f"dec P={dm['precision']} R={dm['recall']}",
            file=sys.stderr,
        )

    n = len(samples)

    def _mean(xs: list[float]) -> float:
        return round(statistics.mean(xs), 3) if xs else 0.0

    return {
        "grounding_rate": _mean(groundings),  # lexical, not semantic faithfulness
        "action_precision": _mean(act_p),
        "action_recall": _mean(act_r),
        "action_f1": _mean(act_f),  # macro: per-sample F1 mean (not F1-of-means)
        "decision_precision": _mean(dec_p),
        "decision_recall": _mean(dec_r),
        "decision_f1": _mean(dec_f),
        "schema_invalid_rate": round(schema_invalid / n, 3) if n else 0.0,
        "format_invalid_rate": round(format_invalid / n, 3) if n else 0.0,
        "backend_error_rate": round(backend_error / n, 3) if n else 0.0,
        "truncation_risk_rate": round(truncation_risk / n, 3) if n else 0.0,
        "p50_ms": round(statistics.median(latencies)) if latencies else 0,
    }


def _settings_for(
    base: Settings,
    model: str,
    seed: int | None,
    num_ctx: int | None,
    keep_alive: str | None,
) -> Settings:
    """Fresh Settings for one (model, seed) eval cell. Non-ollama → base unchanged.

    num_ctx/keep_alive are pinned from the CLI (not re-read from env) so every cell
    is auditably comparable regardless of a stray ``MAI_OLLAMA_*`` in the shell.
    """
    if base.backend != "ollama":
        return base
    kwargs: dict[str, object] = {"backend": "ollama", "ollama_model": model}
    if seed is not None:
        kwargs["ollama_seed"] = seed
    if num_ctx is not None:
        kwargs["ollama_num_ctx"] = num_ctx
    if keep_alive is not None:
        kwargs["ollama_keep_alive"] = keep_alive
    return Settings(**kwargs)  # type: ignore[arg-type]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--eval-set", required=True, help="JSON with samples[]")
    ap.add_argument("--tag", default="", help="label for the JSON result rows")
    ap.add_argument("--match-threshold", type=float, default=0.5)
    ap.add_argument(
        "--models",
        default="",
        help="comma list of ollama model tags to compare "
        "(e.g. 'qwen2.5:7b-instruct,llama3.1:8b'); default = configured model. "
        "Ignored for mock backend.",
    )
    ap.add_argument(
        "--seeds",
        default="",
        help="comma list of int seeds (e.g. '1,2,3'); default = single run. "
        "Multiple seeds measure run-to-run variance (is a model gap real or noise?).",
    )
    ap.add_argument(
        "--num-ctx",
        type=int,
        default=None,
        help="pin Ollama num_ctx for every cell (default = config 8192). Sweep long "
        "meetings with e.g. 8192/16384/32768 to find where truncation_risk clears.",
    )
    ap.add_argument(
        "--keep-alive",
        default="0",
        help="Ollama keep_alive per cell (default '0' = unload after each run so a "
        "model's VRAM residency cannot pollute the next model in a shared-GPU matrix).",
    )
    ap.add_argument(
        "--dataset-kind",
        default="synthetic-neutral",
        choices=(
            "synthetic-neutral",
            "unit-fixture",
            "pilot-meeting",
            "workcube-pilot",
            "customer-pilot",
        ),
        help=(
            "Evidence class for downstream G-INT gate. Defaults to synthetic-neutral; "
            "real pilot acceptance requires an explicit pilot/workcube/customer value."
        ),
    )
    args = ap.parse_args()

    raw = Path(args.eval_set).read_text(encoding="utf-8")
    data = json.loads(raw)
    samples = data.get("samples", [])
    if not samples:
        print(f"NO samples in {args.eval_set}", file=sys.stderr)
        sys.exit(2)

    base = get_settings()
    is_ollama = base.backend == "ollama"
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    if not models or not is_ollama:
        models = [base.ollama_model] if is_ollama else [base.effective_model]
    seeds: list[int | None] = [int(x) for x in args.seeds.split(",") if x.strip()] or [
        base.ollama_seed
    ]
    eval_set_hash = _sha12(raw)
    prompt_hash = _sha12(_OLLAMA_PROMPT)
    # Pin + announce the comparison surface up front so cells are auditably
    # identical (Codex: an env re-read could otherwise make cells incomparable).
    print(
        f"# intel_eval backend={base.backend} models={models} seeds={seeds} "
        f"num_ctx={args.num_ctx or base.ollama_num_ctx} keep_alive={args.keep_alive} "
        f"dataset_kind={args.dataset_kind} eval_set_hash={eval_set_hash} "
        f"prompt_hash={prompt_hash} n={len(samples)}",
        file=sys.stderr,
    )

    for model in models:
        per_seed_runs: list[dict[str, float]] = []
        for seed in seeds:
            settings = _settings_for(base, model, seed, args.num_ctx, args.keep_alive)
            effective_num_ctx = settings.ollama_num_ctx if is_ollama else 0
            svc = MeetingAnalysisService(settings)
            run = _score_run(svc, samples, args.match_threshold, effective_num_ctx, is_ollama)
            row = {
                "tag": args.tag or settings.backend,
                "backend": settings.backend,
                "model": svc.effective_model,
                "seed": seed,
                "eval_set": args.eval_set,
                "dataset_kind": args.dataset_kind,
                "eval_set_hash": eval_set_hash,
                "prompt_hash": prompt_hash,
                "n_samples": len(samples),
                **run,
                # Full effective decoding config + live artifact fingerprint so the
                # row is reproducible even though model tags are mutable (Codex).
                "ollama_options": settings.ollama_options() if is_ollama else None,
                "keep_alive": settings.ollama_keep_alive if is_ollama else None,
                **_ollama_fingerprint(settings),
                "metric_note": "lexical token-overlap; macro per-sample means",
            }
            print(json.dumps(row, ensure_ascii=False))
            per_seed_runs.append(run)
            print(
                f"  == {row['model']} seed={seed}: grounding {run['grounding_rate']:.0%}, "
                f"action P {run['action_precision']:.0%}/R {run['action_recall']:.0%}, "
                f"decision P {run['decision_precision']:.0%}/R {run['decision_recall']:.0%}, "
                f"schema_invalid {run['schema_invalid_rate']:.0%}, "
                f"format_invalid {run['format_invalid_rate']:.0%}, "
                f"backend_err {run['backend_error_rate']:.0%}, "
                f"trunc_risk {run['truncation_risk_rate']:.0%}, p50 {run['p50_ms']}ms",
                file=sys.stderr,
            )

        # Variance summary across seeds — the point of multi-seed: is a model gap
        # real signal or just the run-to-run noise ADR-0034 warned about?
        if len(per_seed_runs) > 1:
            agg: dict[str, object] = {
                "summary": "per-model mean±stdev across seeds",
                "model": model,
                "backend": base.backend,
                "dataset_kind": args.dataset_kind,
                "n_seeds": len(per_seed_runs),
                "eval_set_hash": eval_set_hash,
                "prompt_hash": prompt_hash,
            }
            for k in _METRIC_KEYS:
                vals = [r[k] for r in per_seed_runs]
                agg[f"{k}_mean"] = round(statistics.mean(vals), 3)
                agg[f"{k}_stdev"] = round(statistics.pstdev(vals), 3)
            print(json.dumps(agg, ensure_ascii=False))
            print(
                f"  ## {model} over {agg['n_seeds']} seeds: "
                f"grounding {agg['grounding_rate_mean']:.0%} ±{agg['grounding_rate_stdev']:.1%}, "
                f"decision R {agg['decision_recall_mean']:.0%} "
                f"±{agg['decision_recall_stdev']:.1%}",
                file=sys.stderr,
            )


if __name__ == "__main__":
    main()
