"""Faz 24 T-B G-WER quality gate verifier.

This verifier consumes metadata-only WER and DER evidence rows and decides
whether the Turkish quality gate can be accepted. It deliberately refuses to let
Common Voice or synthetic-smoke rows satisfy the pilot gate: those rows are
useful comparison evidence, but real meeting WER/DER needs privacy-approved
pilot evidence with explicit thresholds.

Output is a redacted JSON summary. Raw audio, transcript text, reference text,
hypothesis text, and file paths are not accepted in evidence rows.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PILOT_KINDS = {"pilot-meeting", "workcube-pilot", "customer-pilot"}
LEGACY_CV_KIND = "legacy-common-voice"
SYNTHETIC_KIND = "synthetic-degraded"

_TEXT_OR_AUDIO_KEYS = {
    "audio",
    "audio_path",
    "audio_url",
    "file",
    "hypothesis",
    "raw",
    "reference",
    "rttm",
    "segments",
    "sentence",
    "text",
    "transcript",
    "transcript_text",
    "wav",
}

_FILE_MARKERS = (".wav", ".rttm", ".mp3", ".flac", ".opus", ".m4a", ".webm", ".ogg")


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


def _kind(row: dict[str, Any], *, metric: str) -> str:
    explicit = row.get("dataset_kind") or row.get("fixture_kind") or row.get("benchmark_kind")
    if explicit:
        return str(explicit)
    tag = str(row.get("tag", "")).lower()
    if metric == "wer" and ("synth" in tag or "degraded" in tag):
        return SYNTHETIC_KIND
    if metric == "wer":
        return LEGACY_CV_KIND
    return "unknown"


def _metric_value(row: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = row.get(key)
        if isinstance(value, int | float):
            return float(value)
    return None


def _privacy_value_findings(value: Any, *, location: str) -> list[str]:
    findings: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            nested_location = f"{location}.{key}"
            if key in _TEXT_OR_AUDIO_KEYS and nested not in (None, "", [], {}):
                findings.append(f"{nested_location} uses disallowed field `{key}`")
            findings.extend(_privacy_value_findings(nested, location=nested_location))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            findings.extend(_privacy_value_findings(nested, location=f"{location}[{index}]"))
    elif isinstance(value, str) and any(marker in value.lower() for marker in _FILE_MARKERS):
        findings.append(f"{location} contains file-path-like value")
    return findings


def _privacy_findings(rows: list[dict[str, Any]], *, label: str) -> list[str]:
    findings: list[str] = []
    for index, row in enumerate(rows, 1):
        for key, value in row.items():
            location = f"{label}[{index}].{key}"
            if key in _TEXT_OR_AUDIO_KEYS and value not in (None, "", [], {}):
                findings.append(f"{label}[{index}] contains disallowed field `{key}`")
            findings.extend(_privacy_value_findings(value, location=location))
    return findings


def _best_row(
    rows: list[dict[str, Any]], *, metric: str, value_keys: tuple[str, ...]
) -> tuple[dict[str, Any] | None, float | None]:
    best: dict[str, Any] | None = None
    best_value: float | None = None
    for row in rows:
        if _kind(row, metric=metric) not in PILOT_KINDS:
            continue
        value = _metric_value(row, value_keys)
        if value is None:
            continue
        if best_value is None or value < best_value:
            best = row
            best_value = value
    return best, best_value


def _summarize_row(
    row: dict[str, Any] | None,
    *,
    metric: str,
    value: float | None,
) -> dict[str, Any] | None:
    if row is None:
        return None
    summary: dict[str, Any] = {
        "tag": row.get("tag"),
        "kind": _kind(row, metric=metric),
        "metric": metric,
        "value": value,
        "n_samples": row.get("n_samples"),
        "rtf": row.get("rtf"),
        "p50_ms": row.get("p50_ms"),
    }
    if metric == "wer":
        summary["model"] = row.get("model")
        summary["compute"] = row.get("compute")
    else:
        summary["backend"] = row.get("backend")
        summary["model"] = row.get("model")
        summary["collar"] = row.get("collar")
    return summary


def evaluate_gate(
    *,
    wer_rows: list[dict[str, Any]],
    der_rows: list[dict[str, Any]],
    max_wer: float | None,
    max_der: float | None,
) -> dict[str, Any]:
    """Return a metadata-only G-WER gate decision."""
    findings: list[str] = []
    findings.extend(_privacy_findings(wer_rows, label="wer"))
    findings.extend(_privacy_findings(der_rows, label="der"))
    if findings:
        return {
            "schema": "faz24.gwer.quality-gate.v1",
            "status": "fail",
            "findingCount": len(findings),
            "findings": findings,
        }

    if max_wer is None or max_der is None:
        findings.append("explicit max_wer and max_der thresholds are required")
    if not wer_rows:
        findings.append("no WER evidence rows supplied")
    if not der_rows:
        findings.append("no DER evidence rows supplied")

    wer_pilot, wer_value = _best_row(wer_rows, metric="wer", value_keys=("wer", "corpus_wer"))
    der_pilot, der_value = _best_row(der_rows, metric="der", value_keys=("der_corpus", "der"))
    wer_kinds = sorted({_kind(row, metric="wer") for row in wer_rows})
    der_kinds = sorted({_kind(row, metric="der") for row in der_rows})

    if wer_rows and wer_pilot is None:
        findings.append(
            "no pilot-meeting WER row; Common Voice/synthetic evidence cannot satisfy G-WER"
        )
    if der_rows and der_pilot is None:
        findings.append("no pilot-meeting DER row; synthetic-smoke evidence cannot satisfy G-WER")

    if findings:
        return {
            "schema": "faz24.gwer.quality-gate.v1",
            "status": "blocked",
            "findingCount": len(findings),
            "findings": findings,
            "werKinds": wer_kinds,
            "derKinds": der_kinds,
            "selectedWer": _summarize_row(wer_pilot, metric="wer", value=wer_value),
            "selectedDer": _summarize_row(der_pilot, metric="der", value=der_value),
        }

    if max_wer is None or max_der is None or wer_value is None or der_value is None:
        return {
            "schema": "faz24.gwer.quality-gate.v1",
            "status": "fail",
            "findingCount": 1,
            "findings": ["internal gate invariant failed after blocked checks"],
        }

    if wer_value > max_wer:
        findings.append(f"pilot WER {wer_value:.4f} exceeds threshold {max_wer:.4f}")
    if der_value > max_der:
        findings.append(f"pilot DER {der_value:.4f} exceeds threshold {max_der:.4f}")

    return {
        "schema": "faz24.gwer.quality-gate.v1",
        "status": "fail" if findings else "pass",
        "findingCount": len(findings),
        "findings": findings,
        "werKinds": wer_kinds,
        "derKinds": der_kinds,
        "thresholds": {
            "maxWer": max_wer,
            "maxDer": max_der,
        },
        "selectedWer": _summarize_row(wer_pilot, metric="wer", value=wer_value),
        "selectedDer": _summarize_row(der_pilot, metric="der", value=der_value),
        "boundary": (
            "PASS covers only Faz 24 T-B Turkish quality gate evidence; it does not "
            "select a model, enable production, or prove direct-STT/app-mTLS gates."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify Faz 24 T-B G-WER quality gate")
    parser.add_argument("--wer-evidence", type=Path, required=True)
    parser.add_argument("--der-evidence", type=Path, required=True)
    parser.add_argument("--max-wer", type=float, default=None)
    parser.add_argument("--max-der", type=float, default=None)
    args = parser.parse_args()

    result = evaluate_gate(
        wer_rows=_load_rows(args.wer_evidence),
        der_rows=_load_rows(args.der_evidence),
        max_wer=args.max_wer,
        max_der=args.max_der,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
