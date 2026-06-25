"""Faz 24 G-LAT/COST quality gate verifier.

This verifier consumes metadata-only latency, queue-lag, throughput, utilization,
and cost evidence rows. It refuses to let synthetic, lab, or Common Voice rows
satisfy the product acceptance gate: those rows are useful operating evidence,
but G-LAT/COST needs privacy-approved pilot evidence with explicit thresholds.

Output is a redacted JSON summary. Raw audio, transcript text, fixture paths,
prompts, responses, and PII-shaped strings are not accepted in evidence rows.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

PILOT_KINDS = {"pilot-meeting", "workcube-pilot", "customer-pilot"}
PILOT_BACKENDS = {
    "audio-gateway",
    "final-stt-service",
    "gpu-host-live-stt",
    "live-stt-service",
}

_DISALLOWED_CONTENT_KEYS = {
    "audio",
    "audio_file",
    "audio_path",
    "audio_url",
    "email",
    "file",
    "file_path",
    "hypothesis",
    "phone",
    "prompt",
    "raw",
    "raw_output",
    "reference",
    "response",
    "samples",
    "segments",
    "sentence",
    "speaker",
    "speaker_name",
    "speakers",
    "source_text",
    "text",
    "transcript",
    "transcript_text",
    "wav",
}
_FILE_MARKERS = (".wav", ".rttm", ".mp3", ".flac", ".opus", ".m4a", ".webm", ".ogg")
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_TC_OR_PHONE_RE = re.compile(r"(?:\+90|0)?5\d{9}\b|\b\d{11}\b")
_TR_IBAN_RE = re.compile(r"\bTR\d{2}(?:[ -]?\d){22}\b", re.IGNORECASE)
_SHA256_RE = re.compile(r"^sha256:[a-f0-9]{64}$", re.IGNORECASE)

_METRIC_KEYS = {
    "audio_minutes",
    "audio_minutes_per_wall_hour",
    "cost_per_audio_minute",
    "cpu_utilization_p95_pct",
    "error_rate",
    "gpu_utilization_p95_pct",
    "latency_p50_ms",
    "latency_p95_ms",
    "queue_lag_p95_ms",
    "realtime_factor",
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
    explicit = row.get("dataset_kind") or row.get("fixture_kind") or row.get("benchmark_kind")
    if explicit:
        return str(explicit)
    tag = str(row.get("tag", "")).lower()
    if "common" in tag or "cv" in tag:
        return "legacy-common-voice"
    if "synthetic" in tag or "fixture" in tag:
        return "synthetic-smoke"
    if "perf" in tag or "gpu" in tag or "matrix" in tag:
        return "perf-lab"
    return "unknown"


def _metric_value(row: dict[str, Any], key: str) -> float | None:
    value = row.get(key)
    if isinstance(value, int | float):
        return float(value)
    return None


def _privacy_value_findings(value: Any, *, location: str) -> list[str]:
    findings: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            nested_location = f"{location}.{key}"
            if key in _DISALLOWED_CONTENT_KEYS and nested not in (None, "", [], {}):
                findings.append(f"{nested_location} uses disallowed content field `{key}`")
            findings.extend(_privacy_value_findings(nested, location=nested_location))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            findings.extend(_privacy_value_findings(nested, location=f"{location}[{index}]"))
    elif isinstance(value, str):
        lowered = value.lower()
        if any(marker in lowered for marker in _FILE_MARKERS):
            findings.append(f"{location} contains file-path-like value")
        if _EMAIL_RE.search(value) or _TC_OR_PHONE_RE.search(value) or _TR_IBAN_RE.search(value):
            findings.append(f"{location} contains PII-shaped value")
    return findings


def _privacy_findings(rows: list[dict[str, Any]]) -> list[str]:
    findings: list[str] = []
    for index, row in enumerate(rows, 1):
        for key, value in row.items():
            location = f"glatCost[{index}].{key}"
            if key in _DISALLOWED_CONTENT_KEYS and value not in (None, "", [], {}):
                findings.append(f"glatCost[{index}] contains disallowed content field `{key}`")
            findings.extend(_privacy_value_findings(value, location=location))
    return findings


def _thresholds(
    *,
    max_latency_p95_ms: float | None,
    max_queue_lag_p95_ms: float | None,
    max_cost_per_audio_minute: float | None,
    max_realtime_factor: float | None,
    max_error_rate: float | None,
    min_audio_minutes: float | None,
    min_audio_minutes_per_wall_hour: float | None,
) -> dict[str, float | None]:
    return {
        "maxLatencyP95Ms": max_latency_p95_ms,
        "maxQueueLagP95Ms": max_queue_lag_p95_ms,
        "maxCostPerAudioMinute": max_cost_per_audio_minute,
        "maxRealtimeFactor": max_realtime_factor,
        "maxErrorRate": max_error_rate,
        "minAudioMinutes": min_audio_minutes,
        "minAudioMinutesPerWallHour": min_audio_minutes_per_wall_hour,
    }


def _candidate_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if any(key in row for key in _METRIC_KEYS)]


def _missing_row_metadata(row: dict[str, Any]) -> list[str]:
    missing = [
        key
        for key in (
            "audio_minutes",
            "audio_minutes_per_wall_hour",
            "cost_per_audio_minute",
            "error_rate",
            "latency_p50_ms",
            "latency_p95_ms",
            "n_samples",
            "queue_lag_p95_ms",
            "realtime_factor",
        )
        if _metric_value(row, key) is None
    ]
    if (
        _metric_value(row, "gpu_utilization_p95_pct") is None
        and _metric_value(row, "cpu_utilization_p95_pct") is None
    ):
        missing.append("gpu_utilization_p95_pct or cpu_utilization_p95_pct")
    for key in ("backend", "compute", "evidence_hash", "model"):
        if not row.get(key):
            missing.append(key)
    return missing


def _pilot_integrity_findings(row: dict[str, Any], *, index: int) -> list[str]:
    findings: list[str] = []
    if _kind(row) not in PILOT_KINDS:
        return findings

    backend = str(row.get("backend", "")).lower()
    if backend not in PILOT_BACKENDS:
        findings.append(
            f"pilot row {index} uses backend={backend or '<missing>'}; "
            "G-LAT/COST pilot needs live-stt-service, final-stt-service, "
            "audio-gateway, or gpu-host-live-stt"
        )

    evidence_hash = str(row.get("evidence_hash", ""))
    if evidence_hash and not _SHA256_RE.fullmatch(evidence_hash):
        findings.append(f"pilot row {index} evidence_hash must be sha256:<64 hex>")

    fixture_hint = str(row.get("evidence_source", "")).replace("\\", "/").lower()
    if "tests/fixtures" in fixture_hint:
        findings.append(
            f"pilot row {index} points at tests/fixtures; fixture rows cannot satisfy G-LAT/COST"
        )
    return findings


def _row_quality_findings(row: dict[str, Any], *, thresholds: dict[str, float | None]) -> list[str]:
    findings: list[str] = []

    max_latency = thresholds["maxLatencyP95Ms"]
    if (
        isinstance(max_latency, int | float)
        and (value := _metric_value(row, "latency_p95_ms")) is not None
        and value > max_latency
    ):
        findings.append(f"pilot latency_p95_ms {value:.2f} exceeds threshold {max_latency:.2f}")

    max_queue = thresholds["maxQueueLagP95Ms"]
    if (
        isinstance(max_queue, int | float)
        and (value := _metric_value(row, "queue_lag_p95_ms")) is not None
        and value > max_queue
    ):
        findings.append(f"pilot queue_lag_p95_ms {value:.2f} exceeds threshold {max_queue:.2f}")

    max_cost = thresholds["maxCostPerAudioMinute"]
    if (
        isinstance(max_cost, int | float)
        and (value := _metric_value(row, "cost_per_audio_minute")) is not None
        and value > max_cost
    ):
        findings.append(f"pilot cost_per_audio_minute {value:.6f} exceeds threshold {max_cost:.6f}")

    max_rtf = thresholds["maxRealtimeFactor"]
    if (
        isinstance(max_rtf, int | float)
        and (value := _metric_value(row, "realtime_factor")) is not None
        and value > max_rtf
    ):
        findings.append(f"pilot realtime_factor {value:.4f} exceeds threshold {max_rtf:.4f}")

    max_error = thresholds["maxErrorRate"]
    if (
        isinstance(max_error, int | float)
        and (value := _metric_value(row, "error_rate")) is not None
        and value > max_error
    ):
        findings.append(f"pilot error_rate {value:.4f} exceeds threshold {max_error:.4f}")

    min_audio_minutes = thresholds["minAudioMinutes"]
    if (
        isinstance(min_audio_minutes, int | float)
        and (value := _metric_value(row, "audio_minutes")) is not None
        and value < min_audio_minutes
    ):
        findings.append(f"pilot audio_minutes {value:.2f} is below minimum {min_audio_minutes:.2f}")

    min_throughput = thresholds["minAudioMinutesPerWallHour"]
    if (
        isinstance(min_throughput, int | float)
        and (value := _metric_value(row, "audio_minutes_per_wall_hour")) is not None
        and value < min_throughput
    ):
        findings.append(
            "pilot audio_minutes_per_wall_hour "
            f"{value:.2f} is below minimum {min_throughput:.2f}"
        )

    return findings


def _quality_score(row: dict[str, Any]) -> float:
    return (
        (_metric_value(row, "latency_p95_ms") or 0.0)
        + (_metric_value(row, "queue_lag_p95_ms") or 0.0)
        + ((_metric_value(row, "cost_per_audio_minute") or 0.0) * 1000.0)
        + ((_metric_value(row, "realtime_factor") or 0.0) * 1000.0)
        + ((_metric_value(row, "error_rate") or 0.0) * 1000.0)
    )


def _summarize_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "tag": row.get("tag"),
        "kind": _kind(row),
        "backend": row.get("backend"),
        "model": row.get("model"),
        "compute": row.get("compute"),
        "n_samples": row.get("n_samples"),
        "audio_minutes": row.get("audio_minutes"),
        "latency_p50_ms": row.get("latency_p50_ms"),
        "latency_p95_ms": row.get("latency_p95_ms"),
        "queue_lag_p95_ms": row.get("queue_lag_p95_ms"),
        "realtime_factor": row.get("realtime_factor"),
        "audio_minutes_per_wall_hour": row.get("audio_minutes_per_wall_hour"),
        "cost_per_audio_minute": row.get("cost_per_audio_minute"),
        "gpu_utilization_p95_pct": row.get("gpu_utilization_p95_pct"),
        "cpu_utilization_p95_pct": row.get("cpu_utilization_p95_pct"),
        "error_rate": row.get("error_rate"),
        "evidence_hash": row.get("evidence_hash"),
    }


def evaluate_gate(
    *,
    rows: list[dict[str, Any]],
    max_latency_p95_ms: float | None,
    max_queue_lag_p95_ms: float | None,
    max_cost_per_audio_minute: float | None,
    max_realtime_factor: float | None,
    max_error_rate: float | None,
    min_audio_minutes: float | None,
    min_audio_minutes_per_wall_hour: float | None,
) -> dict[str, Any]:
    """Return a metadata-only G-LAT/COST gate decision."""
    thresholds = _thresholds(
        max_latency_p95_ms=max_latency_p95_ms,
        max_queue_lag_p95_ms=max_queue_lag_p95_ms,
        max_cost_per_audio_minute=max_cost_per_audio_minute,
        max_realtime_factor=max_realtime_factor,
        max_error_rate=max_error_rate,
        min_audio_minutes=min_audio_minutes,
        min_audio_minutes_per_wall_hour=min_audio_minutes_per_wall_hour,
    )

    findings = _privacy_findings(rows)
    if findings:
        return {
            "schema": "faz24.glat-cost.quality-gate.v1",
            "status": "fail",
            "findingCount": len(findings),
            "findings": findings,
        }

    missing_thresholds = [key for key, value in thresholds.items() if value is None]
    metric_rows = _candidate_rows(rows)
    if missing_thresholds:
        findings.append(
            "explicit G-LAT/COST thresholds are required: " + ", ".join(missing_thresholds)
        )
    if not rows:
        findings.append("no G-LAT/COST evidence rows supplied")
    if rows and not metric_rows:
        findings.append("no latency/cost metric rows supplied")

    kinds = sorted({_kind(row) for row in rows})
    pilot_rows = [row for row in metric_rows if _kind(row) in PILOT_KINDS]
    if metric_rows and not pilot_rows:
        findings.append(
            "no pilot-meeting G-LAT/COST row; synthetic/lab evidence cannot satisfy G-LAT/COST"
        )

    complete_pilot_rows: list[dict[str, Any]] = []
    for index, row in enumerate(pilot_rows, 1):
        integrity_findings = _pilot_integrity_findings(row, index=index)
        findings.extend(integrity_findings)
        missing = _missing_row_metadata(row)
        if missing:
            findings.append(f"pilot row {index} is missing required metadata: {', '.join(missing)}")
        if not integrity_findings and not missing:
            complete_pilot_rows.append(row)

    if pilot_rows and not complete_pilot_rows:
        findings.append("no complete pilot G-LAT/COST row with all required metadata")

    if findings:
        return {
            "schema": "faz24.glat-cost.quality-gate.v1",
            "status": "blocked",
            "findingCount": len(findings),
            "findings": findings,
            "kinds": kinds,
            "thresholds": thresholds,
            "selectedGlatCost": _summarize_row(
                min(complete_pilot_rows, key=_quality_score) if complete_pilot_rows else None
            ),
        }

    selected = min(complete_pilot_rows, key=_quality_score)
    quality_findings = _row_quality_findings(selected, thresholds=thresholds)

    return {
        "schema": "faz24.glat-cost.quality-gate.v1",
        "status": "fail" if quality_findings else "pass",
        "findingCount": len(quality_findings),
        "findings": quality_findings,
        "kinds": kinds,
        "thresholds": thresholds,
        "selectedGlatCost": _summarize_row(selected),
        "boundary": (
            "PASS covers only Faz 24 G-LAT/COST pilot evidence. It does not "
            "select a permanent model/GPU SKU, enable production, prove direct-STT, "
            "or satisfy app-mTLS/KVKK runtime gates."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify Faz 24 G-LAT/COST quality gate")
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--max-latency-p95-ms", type=float, default=None)
    parser.add_argument("--max-queue-lag-p95-ms", type=float, default=None)
    parser.add_argument("--max-cost-per-audio-minute", type=float, default=None)
    parser.add_argument("--max-realtime-factor", type=float, default=None)
    parser.add_argument("--max-error-rate", type=float, default=None)
    parser.add_argument("--min-audio-minutes", type=float, default=None)
    parser.add_argument("--min-audio-minutes-per-wall-hour", type=float, default=None)
    args = parser.parse_args()

    result = evaluate_gate(
        rows=_load_rows(args.evidence),
        max_latency_p95_ms=args.max_latency_p95_ms,
        max_queue_lag_p95_ms=args.max_queue_lag_p95_ms,
        max_cost_per_audio_minute=args.max_cost_per_audio_minute,
        max_realtime_factor=args.max_realtime_factor,
        max_error_rate=args.max_error_rate,
        min_audio_minutes=args.min_audio_minutes,
        min_audio_minutes_per_wall_hour=args.min_audio_minutes_per_wall_hour,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
