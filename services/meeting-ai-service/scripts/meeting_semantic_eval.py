"""Metadata-only synthetic semantic regression through the real analysis service.

MAI_BACKEND=ollama python scripts/meeting_semantic_eval.py --eval-set \
  tests/fixtures/meeting-semantic-gold-v1.json

Uses MAI_* deployment configuration, including mandatory redaction. No mock
fallback. This is a source-level model regression, not an ingress or user journey
acceptance test. stdout contains only hashes, counts, timings and model metadata.
"""

# ruff: noqa: E402, T201

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import statistics
import sys
import time
from pathlib import Path

import httpx

SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

from app.core.config import Settings
from app.services import analyze as analyze_module
from app.services.analyze import MeetingAnalysisService
from app.services.semantic_eval import GoldCorpus, aggregate_scores, load_corpus, score_case


def fingerprint(settings: Settings) -> dict[str, str | None]:
    """Resolve the configured mutable tag to its actual local Ollama digest."""
    result: dict[str, str | None] = {"version": None, "model_digest": None}
    try:
        version = httpx.get(f"{settings.ollama_host}/api/version", timeout=10.0)
        version.raise_for_status()
        tags = httpx.get(f"{settings.ollama_host}/api/tags", timeout=10.0)
        tags.raise_for_status()
        result["version"] = str(version.json()["version"])
        name = settings.ollama_model
        aliases = {name, name + ":latest"} if ":" not in name else {name}
        matches = [model for model in tags.json()["models"] if model.get("name") in aliases]
        if len(matches) == 1 and isinstance(matches[0].get("digest"), str):
            result["model_digest"] = matches[0]["digest"]
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        pass
    return result


def run_corpus(service: MeetingAnalysisService, corpus: GoldCorpus) -> dict[str, object]:
    rows: list[dict[str, object]] = []
    latencies: list[int] = []
    for case in corpus.cases:
        started = time.perf_counter()
        result = None
        error_type = None
        try:
            # Do not forward provider/service stdout into the evidence artifact.
            with contextlib.redirect_stdout(io.StringIO()):
                result = service.analyze(case.transcript)
        except Exception as exc:  # noqa: BLE001 - score failures, never emit exception contents
            error_type = type(exc).__name__
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        print(
            json.dumps({"case_id": case.id, "elapsed_ms": elapsed_ms, "error_type": error_type}),
            file=sys.stderr,
            flush=True,
        )
        latencies.append(elapsed_ms)
        row = score_case(case, result)
        row.update(
            {
                "transcript_sha256": hashlib.sha256(case.transcript.encode()).hexdigest(),
                "elapsed_ms": elapsed_ms,
                "error_type": error_type,
            }
        )
        rows.append(row)
    aggregate = aggregate_scores(rows)
    return {
        "cases": rows,
        "aggregate": aggregate,
        "latency_ms": {"median": statistics.median(latencies), "max": max(latencies)},
    }


def project_gate(report: dict[str, object], model_stable: bool) -> bool:
    aggregate = report["aggregate"]
    if not isinstance(aggregate, dict) or aggregate["error_count"] != 0 or not model_stable:
        return False
    for kind in ("decision", "action", "action_with_metadata"):
        score = aggregate[kind]
        if score["precision"] < 0.90 or score["recall"] < 0.85:
            return False
    return True


def source_fingerprint() -> dict[str, str]:
    return {
        str(path.relative_to(SERVICE_ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((SERVICE_ROOT / "app").rglob("*.py"))
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--eval-set", type=Path, required=True)
    parser.add_argument("--output", type=Path, help="Optional metadata-only JSON artifact")
    args = parser.parse_args()
    corpus = load_corpus(args.eval_set)
    settings = Settings()
    if settings.backend != "ollama":
        parser.error(
            "MAI_BACKEND=ollama is required; mock results are not semantic quality evidence"
        )
    before = fingerprint(settings)
    source_before = source_fingerprint()
    fixture_hash = hashlib.sha256(args.eval_set.read_bytes()).hexdigest()
    report = run_corpus(MeetingAnalysisService(settings), corpus)
    after = fingerprint(settings)
    stable = before == after and bool(before["model_digest"])
    source_after = source_fingerprint()
    inputs_stable = (
        source_before == source_after
        and fixture_hash == hashlib.sha256(args.eval_set.read_bytes()).hexdigest()
    )
    report.update(
        {
            "schema_version": "meeting-semantic-report-v1",
            "evidence_level": "synthetic-source-service-regression-not-user-acceptance",
            "eval_set_sha256": fixture_hash,
            "model": settings.effective_model,
            "backend": settings.backend,
            "redact_pii": settings.redact_pii,
            "ollama_options": settings.ollama_options(),
            "ollama_think": settings.ollama_think,
            "request_timeout_sec": settings.request_timeout,
            "fingerprint_before": before,
            "fingerprint_after": after,
            "model_fingerprint_stable": stable,
            "source_sha256": source_before,
            "source_sha256_after": source_after,
            "inputs_stable": inputs_stable,
            "effective_prompt_sha256": hashlib.sha256(
                analyze_module._OLLAMA_EXTRACTIVE_PROMPT.encode()
            ).hexdigest(),
            "legacy_prompt_sha256": hashlib.sha256(
                analyze_module._OLLAMA_PROMPT.encode()
            ).hexdigest(),
            "project_targets": {"precision": 0.90, "recall": 0.85, "execution_errors": 0},
            "project_gate_passed": project_gate(report, stable and inputs_stable),
        }
    )
    encoded = json.dumps(report, ensure_ascii=True, sort_keys=True)
    if args.output:
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0 if report["project_gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
