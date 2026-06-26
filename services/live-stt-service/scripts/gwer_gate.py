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
import hashlib
import json
import re
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
_SHA256_RE = re.compile(r"^sha256:[a-f0-9]{64}$", re.IGNORECASE)

_PILOT_REQUIRED_METADATA = {
    "wer": (
        "model",
        "compute",
        "n_samples",
        "ref_words",
        "evidence_hash",
        "eval_set_hash",
        "sample_manifest_hash",
        "sample_count_hash",
        "ref_word_count_hash",
    ),
    "der": (
        "backend",
        "model",
        "collar",
        "n_samples",
        "evidence_hash",
        "eval_set_hash",
        "sample_manifest_hash",
        "sample_count_hash",
    ),
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


def _kind(row: dict[str, Any], *, metric: str) -> str:
    explicit = (
        row.get("dataset_kind") or row.get("fixture_kind") or row.get("benchmark_kind")
    )
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


def _sha256_ref(raw: str) -> str:
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _sample_count_hash(eval_set_hash: str, n_samples: int) -> str:
    return _sha256_ref(f"{eval_set_hash}\n{n_samples}\n")


def _ref_word_count_hash(eval_set_hash: str, ref_words: int) -> str:
    return _sha256_ref(f"{eval_set_hash}\n{ref_words}\n")


def _positive_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _missing_pilot_metadata(row: dict[str, Any], *, metric: str) -> list[str]:
    missing: list[str] = []
    for key in _PILOT_REQUIRED_METADATA[metric]:
        value = row.get(key)
        if value is None or value == "":
            missing.append(key)
    if metric == "wer" and _metric_value(row, ("wer", "corpus_wer")) is None:
        missing.append("wer or corpus_wer")
    if metric == "der" and _metric_value(row, ("der_corpus", "der")) is None:
        missing.append("der_corpus or der")
    return missing


def _pilot_integrity_findings(
    row: dict[str, Any], *, metric: str, index: int
) -> list[str]:
    findings: list[str] = []
    if _kind(row, metric=metric) not in PILOT_KINDS:
        return findings
    hash_keys = [
        "evidence_hash",
        "eval_set_hash",
        "sample_manifest_hash",
        "sample_count_hash",
    ]
    if metric == "wer":
        hash_keys.append("ref_word_count_hash")
    for key in hash_keys:
        value = str(row.get(key, ""))
        if value and not _SHA256_RE.fullmatch(value):
            findings.append(f"{metric} pilot row {index} {key} must be sha256:<64 hex>")
    n_samples = _positive_int(row.get("n_samples"))
    if n_samples is None:
        findings.append(
            f"{metric} pilot row {index} n_samples must be a positive integer"
        )
    ref_words = None
    if metric == "wer":
        ref_words = _positive_int(row.get("ref_words"))
        if ref_words is None:
            findings.append(
                f"{metric} pilot row {index} ref_words must be a positive integer"
            )
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
            f"{metric} pilot row {index} "
            "sample_count_hash does not match eval_set_hash+n_samples"
        )
    ref_word_count_hash = row.get("ref_word_count_hash")
    if (
        metric == "wer"
        and ref_words is not None
        and isinstance(eval_set_hash, str)
        and _SHA256_RE.fullmatch(eval_set_hash)
        and isinstance(ref_word_count_hash, str)
        and _SHA256_RE.fullmatch(ref_word_count_hash)
        and ref_word_count_hash != _ref_word_count_hash(eval_set_hash, ref_words)
    ):
        findings.append(
            f"{metric} pilot row {index} "
            "ref_word_count_hash does not match eval_set_hash+ref_words"
        )
    return findings


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
            findings.extend(
                _privacy_value_findings(nested, location=f"{location}[{index}]")
            )
    elif isinstance(value, str) and any(
        marker in value.lower() for marker in _FILE_MARKERS
    ):
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


def _complete_pilot_rows(
    rows: list[dict[str, Any]], *, metric: str, findings: list[str]
) -> list[dict[str, Any]]:
    complete: list[dict[str, Any]] = []
    for index, row in enumerate(
        [
            candidate
            for candidate in rows
            if _kind(candidate, metric=metric) in PILOT_KINDS
        ],
        1,
    ):
        integrity = _pilot_integrity_findings(row, metric=metric, index=index)
        findings.extend(integrity)
        missing = _missing_pilot_metadata(row, metric=metric)
        if missing:
            findings.append(
                f"{metric} pilot row {index} is missing required metadata: "
                + ", ".join(missing)
            )
        if not integrity and not missing:
            complete.append(row)
    return complete


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
        "evidence_hash": row.get("evidence_hash"),
        "eval_set_hash": row.get("eval_set_hash"),
        "sample_manifest_hash": row.get("sample_manifest_hash"),
        "sample_count_hash": row.get("sample_count_hash"),
    }
    if metric == "wer":
        summary["model"] = row.get("model")
        summary["compute"] = row.get("compute")
        summary["ref_words"] = row.get("ref_words")
        summary["ref_word_count_hash"] = row.get("ref_word_count_hash")
    else:
        summary["backend"] = row.get("backend")
        summary["model"] = row.get("model")
        summary["collar"] = row.get("collar")
    return summary


def _thresholds(
    *,
    max_wer: float | None,
    max_der: float | None,
    min_wer_samples: int | None,
    min_der_samples: int | None,
    min_wer_ref_words: int | None,
) -> dict[str, float | int | None]:
    return {
        "maxWer": max_wer,
        "maxDer": max_der,
        "minWerSamples": min_wer_samples,
        "minDerSamples": min_der_samples,
        "minWerRefWords": min_wer_ref_words,
    }


def _quality_findings(
    *,
    wer_row: dict[str, Any],
    der_row: dict[str, Any],
    wer_value: float,
    der_value: float,
    thresholds: dict[str, float | int | None],
) -> list[str]:
    findings: list[str] = []
    max_wer = thresholds["maxWer"]
    max_der = thresholds["maxDer"]
    min_wer_samples = thresholds["minWerSamples"]
    min_der_samples = thresholds["minDerSamples"]
    min_wer_ref_words = thresholds["minWerRefWords"]

    if isinstance(max_wer, int | float) and wer_value > max_wer:
        findings.append(f"pilot WER {wer_value:.4f} exceeds threshold {max_wer:.4f}")
    if isinstance(max_der, int | float) and der_value > max_der:
        findings.append(f"pilot DER {der_value:.4f} exceeds threshold {max_der:.4f}")

    wer_samples = _positive_int(wer_row.get("n_samples"))
    der_samples = _positive_int(der_row.get("n_samples"))
    ref_words = _positive_int(wer_row.get("ref_words"))
    if (
        isinstance(min_wer_samples, int)
        and wer_samples is not None
        and wer_samples < min_wer_samples
    ):
        findings.append(
            f"pilot WER n_samples {wer_samples} is below minimum {min_wer_samples}"
        )
    if (
        isinstance(min_der_samples, int)
        and der_samples is not None
        and der_samples < min_der_samples
    ):
        findings.append(
            f"pilot DER n_samples {der_samples} is below minimum {min_der_samples}"
        )
    if (
        isinstance(min_wer_ref_words, int)
        and ref_words is not None
        and ref_words < min_wer_ref_words
    ):
        findings.append(
            f"pilot WER ref_words {ref_words} is below minimum {min_wer_ref_words}"
        )
    return findings


def evaluate_gate(
    *,
    wer_rows: list[dict[str, Any]],
    der_rows: list[dict[str, Any]],
    max_wer: float | None,
    max_der: float | None,
    min_wer_samples: int | None,
    min_der_samples: int | None,
    min_wer_ref_words: int | None,
) -> dict[str, Any]:
    """Return a metadata-only G-WER gate decision."""
    thresholds = _thresholds(
        max_wer=max_wer,
        max_der=max_der,
        min_wer_samples=min_wer_samples,
        min_der_samples=min_der_samples,
        min_wer_ref_words=min_wer_ref_words,
    )
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

    missing_thresholds = [key for key, value in thresholds.items() if value is None]
    if missing_thresholds:
        findings.append(
            "explicit G-WER/DER thresholds are required: "
            + ", ".join(missing_thresholds)
        )
    if not wer_rows:
        findings.append("no WER evidence rows supplied")
    if not der_rows:
        findings.append("no DER evidence rows supplied")

    wer_kinds = sorted({_kind(row, metric="wer") for row in wer_rows})
    der_kinds = sorted({_kind(row, metric="der") for row in der_rows})
    wer_pilot_rows = [
        row for row in wer_rows if _kind(row, metric="wer") in PILOT_KINDS
    ]
    der_pilot_rows = [
        row for row in der_rows if _kind(row, metric="der") in PILOT_KINDS
    ]

    if wer_rows and not wer_pilot_rows:
        findings.append(
            "no pilot-meeting WER row; Common Voice/synthetic evidence cannot satisfy G-WER"
        )
    if der_rows and not der_pilot_rows:
        findings.append(
            "no pilot-meeting DER row; synthetic-smoke evidence cannot satisfy G-WER"
        )

    complete_wer_rows = _complete_pilot_rows(wer_rows, metric="wer", findings=findings)
    complete_der_rows = _complete_pilot_rows(der_rows, metric="der", findings=findings)
    if wer_pilot_rows and not complete_wer_rows:
        findings.append(
            "no complete pilot WER row with all required metadata and hashes"
        )
    if der_pilot_rows and not complete_der_rows:
        findings.append(
            "no complete pilot DER row with all required metadata and hashes"
        )
    wer_pilot, wer_value = _best_row(
        complete_wer_rows, metric="wer", value_keys=("wer", "corpus_wer")
    )
    der_pilot, der_value = _best_row(
        complete_der_rows, metric="der", value_keys=("der_corpus", "der")
    )

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

    if (
        wer_pilot is None
        or der_pilot is None
        or wer_value is None
        or der_value is None
        or missing_thresholds
    ):
        return {
            "schema": "faz24.gwer.quality-gate.v1",
            "status": "fail",
            "findingCount": 1,
            "findings": ["internal gate invariant failed after blocked checks"],
        }

    findings.extend(
        _quality_findings(
            wer_row=wer_pilot,
            der_row=der_pilot,
            wer_value=wer_value,
            der_value=der_value,
            thresholds=thresholds,
        )
    )

    return {
        "schema": "faz24.gwer.quality-gate.v1",
        "status": "fail" if findings else "pass",
        "findingCount": len(findings),
        "findings": findings,
        "werKinds": wer_kinds,
        "derKinds": der_kinds,
        "thresholds": thresholds,
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
    parser.add_argument("--min-wer-samples", type=int, default=None)
    parser.add_argument("--min-der-samples", type=int, default=None)
    parser.add_argument("--min-wer-ref-words", type=int, default=None)
    args = parser.parse_args()

    result = evaluate_gate(
        wer_rows=_load_rows(args.wer_evidence),
        der_rows=_load_rows(args.der_evidence),
        max_wer=args.max_wer,
        max_der=args.max_der,
        min_wer_samples=args.min_wer_samples,
        min_der_samples=args.min_der_samples,
        min_wer_ref_words=args.min_wer_ref_words,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
