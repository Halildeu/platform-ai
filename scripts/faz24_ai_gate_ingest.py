"""Faz 24 AI product gate evidence ingest wrapper.

The wrapper accepts one redacted JSON envelope and dispatches it to the existing
source-side gate verifiers for G-WER/DER, G-LAT/COST, G-INT, or the
diarization backend decision gate. It performs a fail-closed pre-scan before
invoking verifier code so raw audio, transcripts, prompts, model responses,
secret-shaped values, or PII-shaped values cannot enter the reusable ingest
path.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

WRAPPER_SCHEMA = "faz24.ai-gate-ingest.result.v1"
ENVELOPE_SCHEMA = "faz24.ai-gate-ingest.v1"
SUPPORTED_GATES = {"diar-decision", "glat-cost", "gint", "gwer"}

_AUDIO_FILE_RE = re.compile(
    r"\.(?:wav|rttm|mp3|flac|opus|m4a|webm|ogg)\b", re.IGNORECASE
)
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_TC_OR_PHONE_RE = re.compile(r"\b(?:\+90|0)?5\d{9}\b|\b\d{11}\b")
_TR_IBAN_RE = re.compile(r"\bTR\d{2}(?:[ -]?\d){22}\b", re.IGNORECASE)
_JWT_RE = re.compile(
    r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
)
_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/\-=]+", re.IGNORECASE)
_AUTH_HEADER_RE = re.compile(r"\bAuthorization\s*:", re.IGNORECASE)
_PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_TOKEN_RE = re.compile(
    r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{12,}\b"
    r"|\bgithub_pat_[A-Za-z0-9_]{20,}\b"
    r"|\bsk-[A-Za-z0-9_-]{16,}\b"
    r"|\bxox[baprs]-[A-Za-z0-9-]{12,}\b"
)

_RAW_CONTENT_KEYS = {
    "action_items",
    "actions",
    "attendee",
    "attendees",
    "audio",
    "audio_file",
    "audio_path",
    "audio_url",
    "citation",
    "citations",
    "decision",
    "decisions",
    "email",
    "email_body",
    "expected_actions",
    "expected_decisions",
    "file",
    "file_path",
    "hypothesis",
    "iban",
    "llm_output",
    "name",
    "names",
    "participant",
    "participants",
    "phone",
    "prompt",
    "raw",
    "raw_output",
    "reference",
    "response",
    "rttm",
    "samples",
    "segments",
    "sentence",
    "speaker",
    "speaker_name",
    "speakers",
    "source_quote",
    "source_text",
    "text",
    "transcript",
    "transcript_text",
    "wav",
}
_RAW_KEY_FRAGMENTS = (
    "raw_transcript",
    "prompt_text",
    "response_text",
    "source_quote",
)
_SECRET_KEY_FRAGMENTS = (
    "access_token",
    "api_key",
    "authorization",
    "bearer",
    "client_secret",
    "password",
    "private_key",
    "refresh_token",
    "secret",
)


class EnvelopeError(ValueError):
    """Raised when the ingest envelope is structurally invalid."""


def load_envelope(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise EnvelopeError(f"invalid JSON envelope: {exc.msg}") from exc
    if not isinstance(envelope, dict):
        raise EnvelopeError("envelope JSON root must be an object")
    return envelope


def _canonical_gate(value: Any) -> str:
    gate = str(value or "").strip().lower().replace("_", "-")
    aliases = {
        "glat": "glat-cost",
        "glatcost": "glat-cost",
        "g-lat-cost": "glat-cost",
        "gwer-der": "gwer",
        "g-wer": "gwer",
        "diar": "diar-decision",
        "diarization": "diar-decision",
        "diarization-decision": "diar-decision",
        "diar-decision": "diar-decision",
        "g-diar": "diar-decision",
        "g-int": "gint",
    }
    return aliases.get(gate, gate)


def _is_raw_content_key(key: str) -> bool:
    lowered = key.lower()
    if lowered.endswith("_hash"):
        return False
    return lowered in _RAW_CONTENT_KEYS or any(
        fragment in lowered for fragment in _RAW_KEY_FRAGMENTS
    )


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    if lowered.endswith("_hash"):
        return False
    return any(fragment in lowered for fragment in _SECRET_KEY_FRAGMENTS)


def _scan_value(value: Any, *, location: str) -> list[str]:
    findings: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            nested_location = f"{location}.{key}"
            if _is_raw_content_key(key) and nested not in (None, "", [], {}):
                findings.append(
                    f"{nested_location} uses disallowed raw content field `{key}`"
                )
            if _is_secret_key(key) and nested not in (None, "", [], {}):
                findings.append(
                    f"{nested_location} uses disallowed secret-bearing field `{key}`"
                )
            findings.extend(_scan_value(nested, location=nested_location))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            findings.extend(_scan_value(nested, location=f"{location}[{index}]"))
    elif isinstance(value, str):
        if _AUDIO_FILE_RE.search(value):
            findings.append(f"{location} contains audio/file-path-shaped value")
        if (
            _EMAIL_RE.search(value)
            or _TC_OR_PHONE_RE.search(value)
            or _TR_IBAN_RE.search(value)
        ):
            findings.append(f"{location} contains PII-shaped value")
        if (
            _JWT_RE.search(value)
            or _BEARER_RE.search(value)
            or _AUTH_HEADER_RE.search(value)
            or _PRIVATE_KEY_RE.search(value)
            or _TOKEN_RE.search(value)
        ):
            findings.append(f"{location} contains secret-shaped value")
    return findings


def pre_scan_envelope(envelope: dict[str, Any]) -> list[str]:
    """Return redacted findings for unsafe envelope content."""
    findings: list[str] = []
    schema = envelope.get("schema")
    if schema not in (None, ENVELOPE_SCHEMA):
        findings.append(f"unsupported schema `{schema}`; expected `{ENVELOPE_SCHEMA}`")

    gate = _canonical_gate(envelope.get("gate"))
    if gate not in SUPPORTED_GATES:
        findings.append(
            "unsupported gate; expected one of diar-decision, glat-cost, gint, gwer"
        )

    findings.extend(_scan_value(envelope, location="envelope"))
    return findings


def _load_module(name: str, relative_path: str) -> ModuleType:
    path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise EnvelopeError(f"cannot load verifier module at {relative_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _section(envelope: dict[str, Any], key: str) -> dict[str, Any]:
    value = envelope.get(key, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise EnvelopeError(f"`{key}` must be an object")
    return value


def _rows(container: dict[str, Any], *names: str) -> list[dict[str, Any]]:
    for name in names:
        value = container.get(name)
        if value is None:
            continue
        if not isinstance(value, list):
            raise EnvelopeError(f"`{name}` must be an array of objects")
        if not all(isinstance(row, dict) for row in value):
            raise EnvelopeError(f"`{name}` must contain only objects")
        return value
    return []


def _threshold(thresholds: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in thresholds:
            return thresholds[name]
    return None


def _float_threshold(thresholds: dict[str, Any], *names: str) -> float | None:
    value = _threshold(thresholds, *names)
    if value is None:
        return None
    if not isinstance(value, int | float):
        raise EnvelopeError(f"threshold `{names[0]}` must be numeric")
    return float(value)


def _int_threshold(thresholds: dict[str, Any], *names: str) -> int | None:
    value = _threshold(thresholds, *names)
    if value is None:
        return None
    if not isinstance(value, int):
        raise EnvelopeError(f"threshold `{names[0]}` must be an integer")
    return value


def _dispatch_gwer(envelope: dict[str, Any]) -> dict[str, Any]:
    evidence = _section(envelope, "evidence")
    thresholds = _section(envelope, "thresholds")
    module = _load_module(
        "faz24_gwer_gate",
        "services/live-stt-service/scripts/gwer_gate.py",
    )
    return module.evaluate_gate(
        wer_rows=_rows(evidence, "werRows", "wer_rows"),
        der_rows=_rows(evidence, "derRows", "der_rows"),
        max_wer=_float_threshold(thresholds, "maxWer", "max_wer"),
        max_der=_float_threshold(thresholds, "maxDer", "max_der"),
    )


def _dispatch_glat_cost(envelope: dict[str, Any]) -> dict[str, Any]:
    evidence = _section(envelope, "evidence")
    thresholds = _section(envelope, "thresholds")
    module = _load_module(
        "faz24_glat_cost_gate",
        "services/live-stt-service/scripts/glat_cost_gate.py",
    )
    return module.evaluate_gate(
        rows=_rows(evidence, "rows", "glatCostRows", "glat_cost_rows"),
        max_latency_p95_ms=_float_threshold(
            thresholds, "maxLatencyP95Ms", "max_latency_p95_ms"
        ),
        max_queue_lag_p95_ms=_float_threshold(
            thresholds, "maxQueueLagP95Ms", "max_queue_lag_p95_ms"
        ),
        max_cost_per_audio_minute=_float_threshold(
            thresholds, "maxCostPerAudioMinute", "max_cost_per_audio_minute"
        ),
        max_realtime_factor=_float_threshold(
            thresholds, "maxRealtimeFactor", "max_realtime_factor"
        ),
        max_error_rate=_float_threshold(thresholds, "maxErrorRate", "max_error_rate"),
        min_audio_minutes=_float_threshold(
            thresholds, "minAudioMinutes", "min_audio_minutes"
        ),
        min_audio_minutes_per_wall_hour=_float_threshold(
            thresholds,
            "minAudioMinutesPerWallHour",
            "min_audio_minutes_per_wall_hour",
        ),
    )


def _dispatch_gint(envelope: dict[str, Any]) -> dict[str, Any]:
    evidence = _section(envelope, "evidence")
    thresholds = _section(envelope, "thresholds")
    module = _load_module(
        "faz24_gint_gate",
        "services/meeting-ai-service/scripts/gint_gate.py",
    )
    return module.evaluate_gate(
        rows=_rows(evidence, "rows", "gintRows", "gint_rows"),
        min_grounding_rate=_float_threshold(
            thresholds, "minGroundingRate", "min_grounding_rate"
        ),
        min_action_precision=_float_threshold(
            thresholds, "minActionPrecision", "min_action_precision"
        ),
        min_action_recall=_float_threshold(
            thresholds, "minActionRecall", "min_action_recall"
        ),
        min_decision_precision=_float_threshold(
            thresholds, "minDecisionPrecision", "min_decision_precision"
        ),
        min_decision_recall=_float_threshold(
            thresholds, "minDecisionRecall", "min_decision_recall"
        ),
        max_schema_invalid_rate=_float_threshold(
            thresholds, "maxSchemaInvalidRate", "max_schema_invalid_rate"
        ),
        max_format_invalid_rate=_float_threshold(
            thresholds, "maxFormatInvalidRate", "max_format_invalid_rate"
        ),
        max_backend_error_rate=_float_threshold(
            thresholds, "maxBackendErrorRate", "max_backend_error_rate"
        ),
        max_truncation_risk_rate=_float_threshold(
            thresholds, "maxTruncationRiskRate", "max_truncation_risk_rate"
        ),
        min_samples=_int_threshold(thresholds, "minSamples", "min_samples"),
    )


def _dispatch_diar_decision(envelope: dict[str, Any]) -> dict[str, Any]:
    evidence = _section(envelope, "evidence")
    thresholds = _section(envelope, "thresholds")
    module = _load_module(
        "faz24_diar_decision_gate",
        "services/diarization-service/scripts/diar_decision_gate.py",
    )
    return module.evaluate_gate(
        rows=_rows(evidence, "rows", "diarRows", "diar_rows", "diarizationRows"),
        max_der=_float_threshold(thresholds, "maxDer", "max_der"),
        max_rtf=_float_threshold(thresholds, "maxRtf", "max_rtf"),
        max_latency_ms=_float_threshold(thresholds, "maxLatencyMs", "max_latency_ms"),
        max_peak_vram_delta_mb=_float_threshold(
            thresholds,
            "maxPeakVramDeltaMb",
            "max_peak_vram_delta_mb",
        ),
        min_samples=_int_threshold(thresholds, "minSamples", "min_samples"),
    )


def _wrap_source_result(gate: str, source_result: dict[str, Any]) -> dict[str, Any]:
    status = str(source_result.get("status", "error"))
    findings = source_result.get("findings", [])
    if not isinstance(findings, list):
        findings = ["source verifier returned non-list findings"]
    return {
        "schema": WRAPPER_SCHEMA,
        "gate": gate,
        "status": status if status in {"blocked", "fail", "pass"} else "error",
        "sourceInvoked": True,
        "sourceSchema": source_result.get("schema"),
        "sourceStatus": source_result.get("status"),
        "findingCount": len(findings),
        "findings": findings,
        "sourceReport": source_result,
        "boundary": (
            "This wrapper only validates redacted Faz 24 AI product-gate evidence. "
            "It does not create live pilot acceptance, enable direct-STT, mutate "
            "runtime infrastructure, or make Faz 24 production-ready."
        ),
    }


def _error_result(
    *, gate: str | None, status: str, findings: list[str]
) -> dict[str, Any]:
    return {
        "schema": WRAPPER_SCHEMA,
        "gate": gate,
        "status": status,
        "sourceInvoked": False,
        "findingCount": len(findings),
        "findings": findings,
        "boundary": (
            "The source verifier was not invoked because the ingest envelope did "
            "not pass the wrapper's structural or privacy boundary."
        ),
    }


def evaluate_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    gate = _canonical_gate(envelope.get("gate"))
    findings = pre_scan_envelope(envelope)
    if findings:
        return _error_result(gate=gate or None, status="fail", findings=findings)

    try:
        if gate == "gwer":
            source_result = _dispatch_gwer(envelope)
        elif gate == "glat-cost":
            source_result = _dispatch_glat_cost(envelope)
        elif gate == "gint":
            source_result = _dispatch_gint(envelope)
        elif gate == "diar-decision":
            source_result = _dispatch_diar_decision(envelope)
        else:
            return _error_result(
                gate=gate or None,
                status="fail",
                findings=[
                    "unsupported gate; expected one of diar-decision, glat-cost, gint, gwer"
                ],
            )
    except EnvelopeError as exc:
        return _error_result(gate=gate or None, status="fail", findings=[str(exc)])

    return _wrap_source_result(gate, source_result)


def exit_code(result: dict[str, Any]) -> int:
    if result.get("status") == "pass":
        return 0
    if result.get("status") in {"blocked", "fail"}:
        return 1
    return 2


def write_result(result: dict[str, Any], output_file: Path | None) -> None:
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if output_file is not None:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ingest redacted Faz 24 AI gate evidence"
    )
    parser.add_argument("--evidence-file", type=Path, required=True)
    parser.add_argument("--output-file", type=Path, default=None)
    args = parser.parse_args()

    try:
        envelope = load_envelope(args.evidence_file)
        result = evaluate_envelope(envelope)
    except EnvelopeError as exc:
        result = _error_result(gate=None, status="fail", findings=[str(exc)])

    write_result(result, args.output_file)
    return exit_code(result)


if __name__ == "__main__":
    sys.exit(main())
