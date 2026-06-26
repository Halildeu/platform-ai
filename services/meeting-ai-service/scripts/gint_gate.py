"""Faz 24 T-C G-INT quality gate verifier.

This verifier consumes metadata-only rows from ``scripts/intel_eval.py`` and
decides whether the Intelligence acceptance gate can be accepted. It refuses to
let synthetic/mock evidence satisfy the pilot gate: those rows are useful for CI
and model bakeoffs, but #162 acceptance needs privacy-approved real-meeting
evidence with explicit thresholds.

Output is a redacted JSON summary. Raw transcript text, expected decisions,
actions, prompts, LLM responses, citation quotes, and PII-shaped strings are not
accepted in evidence rows.
"""

# ruff: noqa: T201 - verifier CLI: prints redacted JSON output.

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

PILOT_KINDS = {"pilot-meeting", "workcube-pilot", "customer-pilot"}
PILOT_BACKENDS = {"ollama", "anthropic", "openai"}
SYNTHETIC_NEUTRAL_KIND = "synthetic-neutral"
UNIT_FIXTURE_KIND = "unit-fixture"

_DISALLOWED_CONTENT_KEYS = {
    "action_items",
    "actions",
    "attendee",
    "attendees",
    "citation",
    "citations",
    "decision",
    "decisions",
    "email_body",
    "expected_actions",
    "expected_decisions",
    "hypothesis",
    "iban",
    "llm_output",
    "name",
    "names",
    "participant",
    "participants",
    "prompt",
    "raw",
    "raw_output",
    "reference",
    "response",
    "samples",
    "segments",
    "speaker",
    "speaker_name",
    "speakers",
    "source_text",
    "text",
    "transcript",
    "transcript_text",
}
_DISALLOWED_KEY_FRAGMENTS = (
    "raw_transcript",
    "prompt_text",
    "response_text",
    "source_quote",
)
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_TC_OR_PHONE_RE = re.compile(r"\b(?:\+90|0)?5\d{9}\b|\b\d{11}\b")
_TR_IBAN_RE = re.compile(r"\bTR\d{2}(?:[ -]?\d){22}\b", re.IGNORECASE)
_SHA256_RE = re.compile(r"^sha256:[a-f0-9]{64}$", re.IGNORECASE)

_MIN_METRICS = {
    "grounding_rate": "minGroundingRate",
    "action_precision": "minActionPrecision",
    "action_recall": "minActionRecall",
    "decision_precision": "minDecisionPrecision",
    "decision_recall": "minDecisionRecall",
}
_MAX_METRICS = {
    "schema_invalid_rate": "maxSchemaInvalidRate",
    "format_invalid_rate": "maxFormatInvalidRate",
    "backend_error_rate": "maxBackendErrorRate",
    "truncation_risk_rate": "maxTruncationRiskRate",
}


def _load_rows(path: Path) -> list[dict[str, Any]]:
    """Load evidence from a JSON object, JSON list, or JSONL file."""
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    if raw[0] in "[{":
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            pass
        else:
            if isinstance(data, list):
                return [row for row in data if isinstance(row, dict)]
            if isinstance(data, dict):
                if isinstance(data.get("rows"), list):
                    return [row for row in data["rows"] if isinstance(row, dict)]
                return [data]
            raise ValueError(f"{path}: JSON root must be object or array")

    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(raw.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError(f"{path}:{line_no}: JSONL row must be object")
        rows.append(row)
    return rows


def _kind(row: dict[str, Any]) -> str:
    explicit = (
        row.get("dataset_kind") or row.get("fixture_kind") or row.get("benchmark_kind")
    )
    if explicit:
        return str(explicit)
    if str(row.get("backend", "")).lower() == "mock":
        return UNIT_FIXTURE_KIND
    eval_set = str(row.get("eval_set", "")).lower()
    tag = str(row.get("tag", "")).lower()
    if "tests/fixtures" in eval_set or "synthetic" in tag:
        return SYNTHETIC_NEUTRAL_KIND
    return "unknown"


def _metric_value(row: dict[str, Any], key: str) -> float | None:
    value = row.get(key)
    if isinstance(value, int | float):
        return float(value)
    return None


def _positive_int_value(row: dict[str, Any], key: str) -> int | None:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


def _sha256_ref(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sample_count_hash(eval_set_hash: str, n_samples: int) -> str:
    return _sha256_ref(f"{eval_set_hash}\n{n_samples}\n")


def _is_disallowed_content_key(key: str) -> bool:
    lowered = key.lower()
    if lowered.endswith("_hash"):
        return False
    return lowered in _DISALLOWED_CONTENT_KEYS or any(
        fragment in lowered for fragment in _DISALLOWED_KEY_FRAGMENTS
    )


def _privacy_value_findings(value: Any, *, location: str) -> list[str]:
    findings: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            nested_location = f"{location}.{key}"
            if _is_disallowed_content_key(key) and nested not in (None, "", [], {}):
                findings.append(
                    f"{nested_location} uses disallowed content field `{key}`"
                )
            findings.extend(_privacy_value_findings(nested, location=nested_location))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            findings.extend(
                _privacy_value_findings(nested, location=f"{location}[{index}]")
            )
    elif isinstance(value, str) and (
        _EMAIL_RE.search(value)
        or _TC_OR_PHONE_RE.search(value)
        or _TR_IBAN_RE.search(value)
    ):
        findings.append(f"{location} contains PII-shaped value")
    return findings


def _privacy_findings(rows: list[dict[str, Any]]) -> list[str]:
    findings: list[str] = []
    for index, row in enumerate(rows, 1):
        for key, value in row.items():
            location = f"gint[{index}].{key}"
            if _is_disallowed_content_key(key) and value not in (None, "", [], {}):
                findings.append(
                    f"gint[{index}] contains disallowed content field `{key}`"
                )
            findings.extend(_privacy_value_findings(value, location=location))
    return findings


def _thresholds(
    *,
    min_grounding_rate: float | None,
    min_action_precision: float | None,
    min_action_recall: float | None,
    min_decision_precision: float | None,
    min_decision_recall: float | None,
    max_schema_invalid_rate: float | None,
    max_format_invalid_rate: float | None,
    max_backend_error_rate: float | None,
    max_truncation_risk_rate: float | None,
    min_samples: int | None,
) -> dict[str, float | int | None]:
    return {
        "minGroundingRate": min_grounding_rate,
        "minActionPrecision": min_action_precision,
        "minActionRecall": min_action_recall,
        "minDecisionPrecision": min_decision_precision,
        "minDecisionRecall": min_decision_recall,
        "maxSchemaInvalidRate": max_schema_invalid_rate,
        "maxFormatInvalidRate": max_format_invalid_rate,
        "maxBackendErrorRate": max_backend_error_rate,
        "maxTruncationRiskRate": max_truncation_risk_rate,
        "minSamples": min_samples,
    }


def _candidate_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    metric_keys = set(_MIN_METRICS) | set(_MAX_METRICS)
    return [row for row in rows if any(key in row for key in metric_keys)]


def _missing_row_metrics(row: dict[str, Any]) -> list[str]:
    missing = [key for key in _MIN_METRICS if _metric_value(row, key) is None]
    missing.extend(key for key in _MAX_METRICS if _metric_value(row, key) is None)
    if _metric_value(row, "n_samples") is None:
        missing.append("n_samples")
    if not row.get("eval_set_hash"):
        missing.append("eval_set_hash")
    if not row.get("prompt_hash"):
        missing.append("prompt_hash")
    if not row.get("sample_manifest_hash"):
        missing.append("sample_manifest_hash")
    if not row.get("sample_count_hash"):
        missing.append("sample_count_hash")
    return missing


def _hash_format_findings(row: dict[str, Any], *, index: int) -> list[str]:
    findings: list[str] = []
    for key in (
        "eval_set_hash",
        "prompt_hash",
        "sample_manifest_hash",
        "sample_count_hash",
    ):
        value = row.get(key)
        if isinstance(value, str) and value and not _SHA256_RE.fullmatch(value):
            findings.append(f"pilot row {index} {key} must be sha256:<64 hex>")
    return findings


def _pilot_integrity_findings(row: dict[str, Any], *, index: int) -> list[str]:
    """Catch accidental pilot spoofing before threshold checks.

    This is not a cryptographic attestation. It is a fail-closed guard against the
    common bad evidence shape: taking a synthetic fixture row and editing only
    ``dataset_kind`` to a pilot value. Real pilot rows must come from a non-fixture
    eval-set path and a real backend.
    """
    findings: list[str] = []
    if _kind(row) not in PILOT_KINDS:
        return findings

    backend = str(row.get("backend", "")).lower()
    eval_set = str(row.get("eval_set", "")).replace("\\", "/").lower()
    n_samples = _positive_int_value(row, "n_samples")
    if backend not in PILOT_BACKENDS:
        findings.append(
            f"pilot row {index} uses backend={backend or '<missing>'}; "
            "real G-INT pilot needs ollama, anthropic, or openai"
        )
    if n_samples is None:
        findings.append(f"pilot row {index} n_samples must be a positive integer")
    if not eval_set:
        findings.append(f"pilot row {index} is missing eval_set path")
    if "/tests/fixtures/" in f"/{eval_set}" or eval_set.startswith("tests/fixtures/"):
        findings.append(
            f"pilot row {index} points at tests/fixtures; fixture rows cannot satisfy G-INT"
        )
    findings.extend(_hash_format_findings(row, index=index))
    eval_set_hash = row.get("eval_set_hash")
    sample_count_hash = row.get("sample_count_hash")
    if (
        n_samples is not None
        and isinstance(eval_set_hash, str)
        and _SHA256_RE.fullmatch(eval_set_hash)
        and isinstance(sample_count_hash, str)
        and _SHA256_RE.fullmatch(sample_count_hash)
        and sample_count_hash != _sample_count_hash(eval_set_hash, n_samples)
    ):
        findings.append(
            f"pilot row {index} sample_count_hash does not match eval_set_hash+n_samples"
        )
    return findings


def _row_quality_findings(
    row: dict[str, Any], *, thresholds: dict[str, float | int | None]
) -> list[str]:
    findings: list[str] = []
    n_samples = _metric_value(row, "n_samples")
    min_samples = thresholds["minSamples"]
    if (
        isinstance(min_samples, int)
        and n_samples is not None
        and n_samples < min_samples
    ):
        findings.append(
            f"pilot n_samples {int(n_samples)} is below minimum {min_samples}"
        )

    for row_key, threshold_key in _MIN_METRICS.items():
        value = _metric_value(row, row_key)
        threshold = thresholds[threshold_key]
        if (
            isinstance(threshold, int | float)
            and value is not None
            and value < threshold
        ):
            findings.append(
                f"pilot {row_key} {value:.4f} is below threshold {threshold:.4f}"
            )

    for row_key, threshold_key in _MAX_METRICS.items():
        value = _metric_value(row, row_key)
        threshold = thresholds[threshold_key]
        if (
            isinstance(threshold, int | float)
            and value is not None
            and value > threshold
        ):
            findings.append(
                f"pilot {row_key} {value:.4f} exceeds threshold {threshold:.4f}"
            )

    return findings


def _quality_score(row: dict[str, Any]) -> float:
    positives = sum(_metric_value(row, key) or 0.0 for key in _MIN_METRICS)
    negatives = sum(_metric_value(row, key) or 0.0 for key in _MAX_METRICS)
    return positives - negatives


def _summarize_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "tag": row.get("tag"),
        "kind": _kind(row),
        "backend": row.get("backend"),
        "model": row.get("model"),
        "n_samples": row.get("n_samples"),
        "eval_set_hash": row.get("eval_set_hash"),
        "prompt_hash": row.get("prompt_hash"),
        "sample_manifest_hash": row.get("sample_manifest_hash"),
        "sample_count_hash": row.get("sample_count_hash"),
        "grounding_rate": row.get("grounding_rate"),
        "action_precision": row.get("action_precision"),
        "action_recall": row.get("action_recall"),
        "action_f1": row.get("action_f1"),
        "decision_precision": row.get("decision_precision"),
        "decision_recall": row.get("decision_recall"),
        "decision_f1": row.get("decision_f1"),
        "schema_invalid_rate": row.get("schema_invalid_rate"),
        "format_invalid_rate": row.get("format_invalid_rate"),
        "backend_error_rate": row.get("backend_error_rate"),
        "truncation_risk_rate": row.get("truncation_risk_rate"),
        "p50_ms": row.get("p50_ms"),
        "ollama_options": row.get("ollama_options"),
        "keep_alive": row.get("keep_alive"),
        "ollama_version": row.get("ollama_version"),
        "model_digest": row.get("model_digest"),
    }


def evaluate_gate(
    *,
    rows: list[dict[str, Any]],
    min_grounding_rate: float | None,
    min_action_precision: float | None,
    min_action_recall: float | None,
    min_decision_precision: float | None,
    min_decision_recall: float | None,
    max_schema_invalid_rate: float | None,
    max_format_invalid_rate: float | None,
    max_backend_error_rate: float | None,
    max_truncation_risk_rate: float | None,
    min_samples: int | None,
) -> dict[str, Any]:
    """Return a metadata-only G-INT gate decision."""
    thresholds = _thresholds(
        min_grounding_rate=min_grounding_rate,
        min_action_precision=min_action_precision,
        min_action_recall=min_action_recall,
        min_decision_precision=min_decision_precision,
        min_decision_recall=min_decision_recall,
        max_schema_invalid_rate=max_schema_invalid_rate,
        max_format_invalid_rate=max_format_invalid_rate,
        max_backend_error_rate=max_backend_error_rate,
        max_truncation_risk_rate=max_truncation_risk_rate,
        min_samples=min_samples,
    )
    findings = _privacy_findings(rows)
    if findings:
        return {
            "schema": "faz24.gint.quality-gate.v1",
            "status": "fail",
            "findingCount": len(findings),
            "findings": findings,
        }

    missing_thresholds = [key for key, value in thresholds.items() if value is None]
    metric_rows = _candidate_rows(rows)
    if missing_thresholds:
        findings.append(
            "explicit G-INT thresholds are required: " + ", ".join(missing_thresholds)
        )
    if not rows:
        findings.append("no G-INT evidence rows supplied")
    if rows and not metric_rows:
        findings.append("no intel_eval metric rows supplied")

    pilot_rows = [row for row in metric_rows if _kind(row) in PILOT_KINDS]
    kinds = sorted({_kind(row) for row in rows})
    if metric_rows and not pilot_rows:
        findings.append(
            "no pilot-meeting G-INT row; synthetic/mock evidence cannot satisfy G-INT"
        )

    complete_pilot_rows: list[dict[str, Any]] = []
    for index, row in enumerate(pilot_rows, 1):
        integrity_findings = _pilot_integrity_findings(row, index=index)
        findings.extend(integrity_findings)
        missing = _missing_row_metrics(row)
        if missing:
            findings.append(
                f"pilot row {index} is missing required metadata: {', '.join(missing)}"
            )
        if not integrity_findings and not missing:
            complete_pilot_rows.append(row)

    if pilot_rows and not complete_pilot_rows:
        findings.append(
            "no complete pilot G-INT row with all required metrics and hashes"
        )

    if findings:
        return {
            "schema": "faz24.gint.quality-gate.v1",
            "status": "blocked",
            "findingCount": len(findings),
            "findings": findings,
            "kinds": kinds,
            "thresholds": thresholds,
            "selectedGint": _summarize_row(
                max(complete_pilot_rows, key=_quality_score)
                if complete_pilot_rows
                else None
            ),
        }

    passing_rows = [
        row
        for row in complete_pilot_rows
        if not _row_quality_findings(row, thresholds=thresholds)
    ]
    selected = max(passing_rows or complete_pilot_rows, key=_quality_score)
    quality_findings = _row_quality_findings(selected, thresholds=thresholds)

    return {
        "schema": "faz24.gint.quality-gate.v1",
        "status": "fail" if quality_findings else "pass",
        "findingCount": len(quality_findings),
        "findings": quality_findings,
        "kinds": kinds,
        "thresholds": thresholds,
        "selectedGint": _summarize_row(selected),
        "boundary": (
            "PASS covers only Faz 24 T-C Intelligence G-INT evidence. It does not "
            "enable production, select a permanent LLM provider, prove direct-STT, "
            "or satisfy app-mTLS/KVKK runtime gates."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify Faz 24 T-C G-INT quality gate")
    parser.add_argument("--gint-evidence", type=Path, required=True)
    parser.add_argument("--min-grounding-rate", type=float, default=None)
    parser.add_argument("--min-action-precision", type=float, default=None)
    parser.add_argument("--min-action-recall", type=float, default=None)
    parser.add_argument("--min-decision-precision", type=float, default=None)
    parser.add_argument("--min-decision-recall", type=float, default=None)
    parser.add_argument("--max-schema-invalid-rate", type=float, default=None)
    parser.add_argument("--max-format-invalid-rate", type=float, default=None)
    parser.add_argument("--max-backend-error-rate", type=float, default=None)
    parser.add_argument("--max-truncation-risk-rate", type=float, default=None)
    parser.add_argument("--min-samples", type=int, default=None)
    args = parser.parse_args()

    result = evaluate_gate(
        rows=_load_rows(args.gint_evidence),
        min_grounding_rate=args.min_grounding_rate,
        min_action_precision=args.min_action_precision,
        min_action_recall=args.min_action_recall,
        min_decision_precision=args.min_decision_precision,
        min_decision_recall=args.min_decision_recall,
        max_schema_invalid_rate=args.max_schema_invalid_rate,
        max_format_invalid_rate=args.max_format_invalid_rate,
        max_backend_error_rate=args.max_backend_error_rate,
        max_truncation_risk_rate=args.max_truncation_risk_rate,
        min_samples=args.min_samples,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
