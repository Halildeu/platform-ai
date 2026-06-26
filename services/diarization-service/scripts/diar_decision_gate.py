"""Faz 24 T-B diarization backend decision gate verifier.

This verifier consumes metadata-only diarization candidate rows and decides
whether a backend decision can be accepted. It deliberately refuses to let
synthetic-smoke, Common Voice, or lab rows select a permanent diarization
backend: #161 needs privacy-approved pilot evidence, explicit thresholds,
approved license/deployment metadata, and an explicit non-biometric posture.

Output is a redacted JSON summary. Raw audio, RTTM, transcript text, embeddings,
speaker identity data, and file paths are not accepted in evidence rows.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

SCHEMA = "faz24.diarization-decision-gate.v1"
PILOT_KINDS = {"pilot-meeting", "workcube-pilot", "customer-pilot"}
BACKENDS = {"nemo", "pyannote", "speechbrain"}
APPROVED_LICENSE_STATUSES = {
    "approved",
    "commercial-approved",
    "internal-approved",
    "legal-approved",
}
SELF_HOST_DEPLOYMENT_MODES = {"on-prem", "private-vpc", "self-host", "self-hosted"}

_DISALLOWED_CONTENT_KEYS = {
    "audio",
    "audio_file",
    "audio_path",
    "audio_url",
    "embedding",
    "embeddings",
    "file",
    "file_path",
    "hypothesis",
    "name",
    "names",
    "participant",
    "participants",
    "raw",
    "reference",
    "response",
    "rttm",
    "samples",
    "segments",
    "sentence",
    "speaker",
    "speaker_embedding",
    "speaker_name",
    "speakers",
    "text",
    "transcript",
    "transcript_text",
    "wav",
}
_FILE_MARKERS = (".wav", ".rttm", ".mp3", ".flac", ".opus", ".m4a", ".webm", ".ogg")
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_TC_OR_PHONE_RE = re.compile(r"\b(?:\+90|0)?5\d{9}\b|\b\d{11}\b")
_TR_IBAN_RE = re.compile(r"\bTR\d{2}(?:[ -]?\d){22}\b", re.IGNORECASE)
_SHA256_RE = re.compile(r"^sha256:[a-f0-9]{64}$", re.IGNORECASE)


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
    tag = str(row.get("tag", "")).lower()
    if "pilot" in tag:
        return "pilot-meeting"
    if "common" in tag or "cv" in tag:
        return "legacy-common-voice"
    if "synthetic" in tag or "fixture" in tag:
        return "synthetic-smoke"
    if "perf" in tag or "matrix" in tag:
        return "perf-lab"
    return "unknown"


def _metric_value(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = row.get(key)
        if isinstance(value, int | float):
            return float(value)
    return None


def _bool_value(row: dict[str, Any], key: str) -> bool | None:
    value = row.get(key)
    if isinstance(value, bool):
        return value
    return None


def _privacy_value_findings(value: Any, *, location: str) -> list[str]:
    findings: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            nested_location = f"{location}.{key}"
            if key in _DISALLOWED_CONTENT_KEYS and nested not in (None, "", [], {}):
                findings.append(
                    f"{nested_location} uses disallowed content field `{key}`"
                )
            findings.extend(_privacy_value_findings(nested, location=nested_location))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            findings.extend(
                _privacy_value_findings(nested, location=f"{location}[{index}]")
            )
    elif isinstance(value, str):
        lowered = value.lower()
        if any(marker in lowered for marker in _FILE_MARKERS):
            findings.append(f"{location} contains file-path-like value")
        if (
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
            location = f"diarization[{index}].{key}"
            if key in _DISALLOWED_CONTENT_KEYS and value not in (None, "", [], {}):
                findings.append(
                    f"diarization[{index}] contains disallowed content field `{key}`"
                )
            findings.extend(_privacy_value_findings(value, location=location))
    return findings


def _thresholds(
    *,
    max_der: float | None,
    max_rtf: float | None,
    max_latency_ms: float | None,
    max_peak_vram_delta_mb: float | None,
    min_samples: int | None,
) -> dict[str, float | int | None]:
    return {
        "maxDer": max_der,
        "maxRtf": max_rtf,
        "maxLatencyMs": max_latency_ms,
        "maxPeakVramDeltaMb": max_peak_vram_delta_mb,
        "minSamples": min_samples,
    }


def _candidate_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if row.get("backend") or _metric_value(row, "der_corpus", "der") is not None
    ]


def _hard_policy_findings(row: dict[str, Any], *, index: int) -> list[str]:
    findings: list[str] = []
    backend = str(row.get("backend", "")).lower()
    if backend and backend not in BACKENDS:
        findings.append(f"row {index} uses unsupported backend `{backend}`")

    license_status = str(row.get("license_status", "")).lower()
    if license_status in {"forbidden", "rejected", "unknown-prod-risk"}:
        findings.append(
            f"row {index} license_status={license_status} is not acceptable"
        )

    for key in (
        "biometric_processing",
        "speaker_identity_mapping",
        "voiceprint_enabled",
    ):
        if _bool_value(row, key) is True:
            findings.append(
                f"row {index} sets {key}=true; #161 decision gate is non-biometric"
            )

    deployment_mode = str(row.get("deployment_mode", "")).lower()
    if deployment_mode in {"public-cloud", "third-party-saas", "external-api"}:
        findings.append(
            f"row {index} deployment_mode={deployment_mode} crosses the self-host boundary"
        )
    return findings


def _missing_pilot_metadata(row: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for key in (
        "backend",
        "deployment_mode",
        "device",
        "evidence_hash",
        "license_status",
        "model",
    ):
        if not row.get(key):
            missing.append(key)
    for key in (
        "biometric_processing",
        "speaker_identity_mapping",
        "voiceprint_enabled",
    ):
        if _bool_value(row, key) is None:
            missing.append(key)
    for label, keys in {
        "der_corpus or der": ("der_corpus", "der"),
        "lat_max_ms or latency_p95_ms or p95_ms": (
            "lat_max_ms",
            "latency_p95_ms",
            "p95_ms",
        ),
        "n_samples": ("n_samples",),
        "peak_vram_delta_mb": ("peak_vram_delta_mb",),
        "rtf": ("rtf",),
    }.items():
        if _metric_value(row, *keys) is None:
            missing.append(label)
    return missing


def _pilot_integrity_findings(row: dict[str, Any], *, index: int) -> list[str]:
    findings: list[str] = []
    if _kind(row) not in PILOT_KINDS:
        return findings

    license_status = str(row.get("license_status", "")).lower()
    if license_status and license_status not in APPROVED_LICENSE_STATUSES:
        findings.append(
            f"pilot row {index} license_status={license_status} is not approved"
        )

    deployment_mode = str(row.get("deployment_mode", "")).lower()
    if deployment_mode and deployment_mode not in SELF_HOST_DEPLOYMENT_MODES:
        findings.append(
            f"pilot row {index} deployment_mode={deployment_mode} is not self-host"
        )

    evidence_hash = str(row.get("evidence_hash", ""))
    if evidence_hash and not _SHA256_RE.fullmatch(evidence_hash):
        findings.append(f"pilot row {index} evidence_hash must be sha256:<64 hex>")
    return findings


def _quality_findings(
    row: dict[str, Any],
    *,
    thresholds: dict[str, float | int | None],
) -> list[str]:
    findings: list[str] = []
    der = _metric_value(row, "der_corpus", "der")
    max_der = thresholds["maxDer"]
    if isinstance(max_der, int | float) and der is not None and der > max_der:
        findings.append(f"pilot DER {der:.4f} exceeds threshold {float(max_der):.4f}")

    rtf = _metric_value(row, "rtf")
    max_rtf = thresholds["maxRtf"]
    if isinstance(max_rtf, int | float) and rtf is not None and rtf > max_rtf:
        findings.append(f"pilot RTF {rtf:.4f} exceeds threshold {float(max_rtf):.4f}")

    latency = _metric_value(row, "lat_max_ms", "latency_p95_ms", "p95_ms")
    max_latency = thresholds["maxLatencyMs"]
    if (
        isinstance(max_latency, int | float)
        and latency is not None
        and latency > max_latency
    ):
        findings.append(
            f"pilot latency {latency:.2f}ms exceeds threshold {float(max_latency):.2f}ms"
        )

    vram = _metric_value(row, "peak_vram_delta_mb")
    max_vram = thresholds["maxPeakVramDeltaMb"]
    if isinstance(max_vram, int | float) and vram is not None and vram > max_vram:
        findings.append(
            f"pilot peak_vram_delta_mb {vram:.2f} exceeds threshold {float(max_vram):.2f}"
        )

    n_samples = _metric_value(row, "n_samples")
    min_samples = thresholds["minSamples"]
    if (
        isinstance(min_samples, int)
        and n_samples is not None
        and n_samples < min_samples
    ):
        findings.append(
            f"pilot n_samples {n_samples:.0f} is below minimum {min_samples}"
        )
    return findings


def _quality_score(row: dict[str, Any]) -> float:
    return (
        (_metric_value(row, "der_corpus", "der") or 0.0) * 10000.0
        + (_metric_value(row, "rtf") or 0.0) * 1000.0
        + (_metric_value(row, "lat_max_ms", "latency_p95_ms", "p95_ms") or 0.0)
        + (_metric_value(row, "peak_vram_delta_mb") or 0.0)
    )


def _summarize_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "tag": row.get("tag"),
        "kind": _kind(row),
        "backend": row.get("backend"),
        "model": row.get("model"),
        "revision": row.get("revision"),
        "device": row.get("device"),
        "deployment_mode": row.get("deployment_mode"),
        "license_status": row.get("license_status"),
        "n_samples": row.get("n_samples"),
        "der_corpus": row.get("der_corpus"),
        "der": row.get("der"),
        "collar": row.get("collar"),
        "skip_overlap": row.get("skip_overlap"),
        "latency_ms": _metric_value(row, "lat_max_ms", "latency_p95_ms", "p95_ms"),
        "rtf": row.get("rtf"),
        "peak_vram_delta_mb": row.get("peak_vram_delta_mb"),
        "evidence_hash": row.get("evidence_hash"),
    }


def evaluate_gate(
    *,
    rows: list[dict[str, Any]],
    max_der: float | None,
    max_rtf: float | None,
    max_latency_ms: float | None,
    max_peak_vram_delta_mb: float | None,
    min_samples: int | None,
) -> dict[str, Any]:
    """Return a metadata-only diarization backend decision gate result."""
    thresholds = _thresholds(
        max_der=max_der,
        max_rtf=max_rtf,
        max_latency_ms=max_latency_ms,
        max_peak_vram_delta_mb=max_peak_vram_delta_mb,
        min_samples=min_samples,
    )

    findings = _privacy_findings(rows)
    if findings:
        return {
            "schema": SCHEMA,
            "status": "fail",
            "findingCount": len(findings),
            "findings": findings,
        }

    missing_thresholds = [key for key, value in thresholds.items() if value is None]
    if missing_thresholds:
        findings.append(
            "explicit diarization decision thresholds are required: "
            + ", ".join(missing_thresholds)
        )
    if not rows:
        findings.append("no diarization candidate rows supplied")

    candidate_rows = _candidate_rows(rows)
    if rows and not candidate_rows:
        findings.append("no diarization metric candidate rows supplied")

    kinds = sorted({_kind(row) for row in rows})
    policy_findings: list[str] = []
    for index, row in enumerate(candidate_rows, 1):
        policy_findings.extend(_hard_policy_findings(row, index=index))
    if policy_findings:
        return {
            "schema": SCHEMA,
            "status": "fail",
            "findingCount": len(policy_findings),
            "findings": policy_findings,
            "kinds": kinds,
            "thresholds": thresholds,
            "selectedDiarization": None,
        }

    pilot_rows = [row for row in candidate_rows if _kind(row) in PILOT_KINDS]
    if candidate_rows and not pilot_rows:
        findings.append(
            "no pilot-meeting diarization row; synthetic/lab evidence cannot select a backend"
        )

    complete_pilot_rows: list[dict[str, Any]] = []
    for index, row in enumerate(pilot_rows, 1):
        integrity_findings = _pilot_integrity_findings(row, index=index)
        findings.extend(integrity_findings)
        missing = _missing_pilot_metadata(row)
        if missing:
            findings.append(
                f"pilot row {index} is missing required metadata: {', '.join(missing)}"
            )
        if not integrity_findings and not missing:
            complete_pilot_rows.append(row)

    if pilot_rows and not complete_pilot_rows:
        findings.append("no complete pilot diarization row with all decision metadata")

    if findings:
        return {
            "schema": SCHEMA,
            "status": "blocked",
            "findingCount": len(findings),
            "findings": findings,
            "kinds": kinds,
            "thresholds": thresholds,
            "selectedDiarization": _summarize_row(
                min(complete_pilot_rows, key=_quality_score)
                if complete_pilot_rows
                else None
            ),
        }

    selected = min(complete_pilot_rows, key=_quality_score)
    quality_findings = _quality_findings(selected, thresholds=thresholds)

    return {
        "schema": SCHEMA,
        "status": "fail" if quality_findings else "pass",
        "findingCount": len(quality_findings),
        "findings": quality_findings,
        "kinds": kinds,
        "thresholds": thresholds,
        "selectedDiarization": _summarize_row(selected),
        "boundary": (
            "PASS covers only the source-side Faz 24 #161 diarization backend "
            "decision evidence. It does not process real audio, enable voiceprint, "
            "mutate runtime, prove direct-STT/app-mTLS, or make Faz 24 production-ready."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify Faz 24 diarization backend decision gate"
    )
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--max-der", type=float, default=None)
    parser.add_argument("--max-rtf", type=float, default=None)
    parser.add_argument("--max-latency-ms", type=float, default=None)
    parser.add_argument("--max-peak-vram-delta-mb", type=float, default=None)
    parser.add_argument("--min-samples", type=int, default=None)
    args = parser.parse_args()

    result = evaluate_gate(
        rows=_load_rows(args.evidence),
        max_der=args.max_der,
        max_rtf=args.max_rtf,
        max_latency_ms=args.max_latency_ms,
        max_peak_vram_delta_mb=args.max_peak_vram_delta_mb,
        min_samples=args.min_samples,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
