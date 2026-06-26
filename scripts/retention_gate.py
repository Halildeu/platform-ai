"""Faz 24 KVKK retention readiness gate.

This metadata-only verifier checks whether #156 retention/automatic-destruction
evidence is ready for go-live. It deliberately separates MinIO lifecycle source
configuration from runtime lifecycle export evidence so missing object-store
proof cannot be hidden by optimistic documentation.
"""

# ruff: noqa: T201 - verifier CLI prints JSON output.

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

SCHEMA = "faz24.retention-readiness.v1"

EXPECTED_LAYERS: dict[str, dict[str, Any]] = {
    "minio.meeting-audio": {
        "retention_days": 7,
        "mechanism": "s3-lifecycle",
        "source_bucket": "meeting-audio",
    },
    "minio.transcripts": {
        "retention_days": 365,
        "mechanism": "s3-lifecycle",
        "source_bucket": "transcripts",
    },
    "minio.audit-archive": {
        "retention_days": 2557,
        "mechanism": "s3-lifecycle",
        "source_bucket": "audit-archive",
    },
    "db.transcript-records": {
        "retention_days": 365,
        "mechanism": "db-cleanup-job",
        "tenant_id_field": "tenant_id",
        "retention_timestamp_field": "created_at",
    },
    "db.meeting-intelligence": {
        "retention_days": 730,
        "mechanism": "db-cleanup-job",
        "tenant_id_field": "tenant_id",
        "retention_timestamp_field": "created_at",
    },
    "db.kvkk-access-log": {
        "retention_days": 730,
        "mechanism": "db-cleanup-job",
        "tenant_id_field": "tenant_id",
        "retention_timestamp_field": "accessed_at",
    },
}

MINIO_LAYER_IDS = {
    "minio.meeting-audio",
    "minio.transcripts",
    "minio.audit-archive",
}
DB_LAYER_IDS = {
    "db.transcript-records",
    "db.meeting-intelligence",
    "db.kvkk-access-log",
}
PASS_VERBIS_STATUSES = {"recorded", "exempt-confirmed"}
VALID_LAYER_STATUSES = {
    "active",
    "pending",
    "blocked",
    "not-implemented",
    "not-applicable",
}
TRANSCRIPT_FREE_AUDIT_VALUES = {"metadata-only", "transcript-free", "id-only"}

_DISALLOWED_CONTENT_KEYS = {
    "action",
    "actions",
    "audio",
    "audio_path",
    "audio_url",
    "citation",
    "citations",
    "decision",
    "decisions",
    "email",
    "email_body",
    "file_path",
    "hypothesis",
    "iban",
    "participant",
    "participant_name",
    "participants",
    "prompt",
    "raw",
    "raw_audio",
    "raw_output",
    "response",
    "segments",
    "speaker",
    "speaker_name",
    "summary",
    "text",
    "token",
    "transcript",
    "transcript_text",
}
_DISALLOWED_KEY_FRAGMENTS = (
    "access_key",
    "authorization",
    "bearer",
    "credential",
    "password",
    "raw_transcript",
    "prompt_text",
    "response_text",
    "secret",
    "source_quote",
)
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_TC_OR_PHONE_RE = re.compile(r"\b(?:\+90|0)?5\d{9}\b|\b[1-9]\d{10}\b")
_TR_IBAN_RE = re.compile(r"\bTR\d{2}(?:[ -]?\d){22}\b", re.IGNORECASE)
_SECRET_VALUE_RE = re.compile(
    r"(?:\bBearer\s+[A-Za-z0-9._~+/=-]{16,}|"
    r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}|"
    r"\bAKIA[0-9A-Z]{16}\b|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----)",
    re.IGNORECASE,
)


def _load_document(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        raise ValueError(f"{path}: empty evidence file")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: evidence root must be a JSON object")
    return data


def _is_disallowed_content_key(key: str) -> bool:
    lowered = key.lower()
    if (
        lowered.endswith("_hash")
        or lowered.endswith("_ref")
        or lowered == "evidence_ref"
    ):
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
    elif isinstance(value, str):
        if (
            _EMAIL_RE.search(value)
            or _TC_OR_PHONE_RE.search(value)
            or _TR_IBAN_RE.search(value)
        ):
            findings.append(f"{location} contains PII-shaped value")
        if _SECRET_VALUE_RE.search(value):
            findings.append(f"{location} contains secret-shaped value")
    return findings


def _privacy_findings(document: dict[str, Any]) -> list[str]:
    findings: list[str] = []
    for key, value in document.items():
        location = f"evidence.{key}"
        if _is_disallowed_content_key(key) and value not in (None, "", [], {}):
            findings.append(f"evidence contains disallowed content field `{key}`")
        findings.extend(_privacy_value_findings(value, location=location))
    return findings


def _layers_by_id(
    document: dict[str, Any], findings: list[str]
) -> dict[str, dict[str, Any]]:
    raw_layers = document.get("layers")
    if not isinstance(raw_layers, list):
        findings.append("layers must be a JSON array")
        return {}

    layers: dict[str, dict[str, Any]] = {}
    for index, raw_layer in enumerate(raw_layers, 1):
        if not isinstance(raw_layer, dict):
            findings.append(f"layers[{index}] must be an object")
            continue
        raw_id = raw_layer.get("id")
        if not isinstance(raw_id, str) or not raw_id:
            findings.append(f"layers[{index}] is missing id")
            continue
        if raw_id in layers:
            findings.append(f"duplicate retention layer `{raw_id}`")
            continue
        layers[raw_id] = raw_layer
    return layers


def _source_assertions(
    repo_root: Path | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    if repo_root is None:
        return [], []

    assertions: list[dict[str, Any]] = []
    findings: list[str] = []
    lifecycle_script = repo_root / "deploy" / "minio" / "setup-lifecycle.sh"
    if not lifecycle_script.exists():
        return assertions, ["deploy/minio/setup-lifecycle.sh is missing"]

    source = lifecycle_script.read_text(encoding="utf-8")
    for layer_id, expected in EXPECTED_LAYERS.items():
        bucket = expected.get("source_bucket")
        if not bucket:
            continue
        days = expected["retention_days"]
        ok = (
            re.search(rf"\badd_if_absent\s+{re.escape(bucket)}\s+{days}\b", source)
            is not None
        )
        assertions.append(
            {
                "layer": layer_id,
                "source": "deploy/minio/setup-lifecycle.sh",
                "expected": f"{bucket}:{days}d",
                "status": "pass" if ok else "fail",
            }
        )
        if not ok:
            findings.append(
                f"{layer_id} source lifecycle does not assert {bucket}:{days}d"
            )
    return assertions, findings


def _require_non_empty(
    layer: dict[str, Any], key: str, layer_id: str, findings: list[str]
) -> None:
    if not layer.get(key):
        findings.append(f"{layer_id} active claim is missing {key}")


def _evaluate_minio_layer(
    *,
    layer_id: str,
    layer: dict[str, Any],
    expected: dict[str, Any],
    findings: list[str],
) -> None:
    if layer.get("bucket_name") != expected["source_bucket"]:
        findings.append(f"{layer_id} bucket_name must be {expected['source_bucket']}")
    if layer.get("observed_expiration_days") != expected["retention_days"]:
        findings.append(
            f"{layer_id} observed_expiration_days must be {expected['retention_days']}"
        )
    if str(layer.get("observed_rule_status", "")).lower() != "enabled":
        findings.append(f"{layer_id} observed_rule_status must be Enabled")
    if layer.get("evidence_payload") != "metadata-only":
        findings.append(f"{layer_id} must declare evidence_payload=metadata-only")

    _require_non_empty(layer, "runtime_environment", layer_id, findings)
    _require_non_empty(layer, "lifecycle_export_ref", layer_id, findings)
    _require_non_empty(layer, "runtime_evidence_ref", layer_id, findings)


def _evaluate_db_layer(
    *,
    layer_id: str,
    layer: dict[str, Any],
    expected: dict[str, Any],
    findings: list[str],
) -> None:
    _require_non_empty(layer, "persistence_ref", layer_id, findings)
    _require_non_empty(layer, "cleanup_job_ref", layer_id, findings)
    _require_non_empty(layer, "destruction_audit_ref", layer_id, findings)
    if layer.get("tenant_id_field") != expected["tenant_id_field"]:
        findings.append(
            f"{layer_id} must declare tenant_id_field={expected['tenant_id_field']}"
        )
    if layer.get("retention_timestamp_field") != expected["retention_timestamp_field"]:
        findings.append(
            f"{layer_id} must declare retention_timestamp_field="
            f"{expected['retention_timestamp_field']}"
        )
    if layer.get("audit_payload") not in TRANSCRIPT_FREE_AUDIT_VALUES:
        findings.append(
            f"{layer_id} must prove transcript-free destruction audit payload"
        )


def _evaluate_layer(
    *,
    layer_id: str,
    layer: dict[str, Any],
    expected: dict[str, Any],
    findings: list[str],
    blockers: list[str],
) -> None:
    status = str(layer.get("status", "")).lower()
    if status not in VALID_LAYER_STATUSES:
        findings.append(f"{layer_id} has invalid status `{status or '<missing>'}`")
        return
    if status != "active":
        blockers.append(f"{layer_id} status={status}; go-live needs active evidence")
        return

    if layer.get("mechanism") != expected["mechanism"]:
        findings.append(f"{layer_id} mechanism must be {expected['mechanism']}")
    if layer.get("retention_days") != expected["retention_days"]:
        findings.append(
            f"{layer_id} retention_days must be {expected['retention_days']}"
        )
    _require_non_empty(layer, "evidence_ref", layer_id, findings)

    if layer_id in MINIO_LAYER_IDS:
        _evaluate_minio_layer(
            layer_id=layer_id,
            layer=layer,
            expected=expected,
            findings=findings,
        )
        return

    if layer_id in DB_LAYER_IDS:
        _evaluate_db_layer(
            layer_id=layer_id,
            layer=layer,
            expected=expected,
            findings=findings,
        )
        return

    findings.append(f"{layer_id} has no evaluator")


def evaluate_gate(
    document: dict[str, Any], *, repo_root: Path | None = None
) -> dict[str, Any]:
    """Return a metadata-only retention readiness decision."""
    findings: list[str] = []
    blockers: list[str] = []

    findings.extend(_privacy_findings(document))
    if document.get("schema") != SCHEMA:
        findings.append(f"schema must be {SCHEMA}")
    if not document.get("evidence_id"):
        blockers.append("evidence_id is required")

    verbis = document.get("verbis")
    verbis_status = ""
    if isinstance(verbis, dict):
        verbis_status = str(verbis.get("status", "")).lower()
    if verbis_status not in PASS_VERBIS_STATUSES:
        blockers.append(
            "VERBIS status must be recorded or exempt-confirmed before go-live"
        )

    layers = _layers_by_id(document, findings)
    for layer_id, expected in EXPECTED_LAYERS.items():
        layer = layers.get(layer_id)
        if layer is None:
            blockers.append(f"{layer_id} evidence is missing")
            continue
        _evaluate_layer(
            layer_id=layer_id,
            layer=layer,
            expected=expected,
            findings=findings,
            blockers=blockers,
        )

    source_assertions, source_findings = _source_assertions(repo_root)
    findings.extend(source_findings)

    status = "pass"
    if findings:
        status = "fail"
    elif blockers:
        status = "blocked"

    return {
        "schema": SCHEMA,
        "status": status,
        "findingCount": len(findings),
        "blockerCount": len(blockers),
        "findings": findings,
        "blockers": blockers,
        "requiredLayers": list(EXPECTED_LAYERS),
        "sourceAssertions": source_assertions,
        "boundary": (
            "PASS covers only Faz 24 #156 retention readiness evidence. It does not "
            "prove direct-STT, G-WER, G-INT, app-mTLS, legal sign-off, or production "
            "runtime acceptance."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify Faz 24 #156 retention readiness"
    )
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Optional repository root for MinIO lifecycle source assertions",
    )
    args = parser.parse_args()

    result = evaluate_gate(_load_document(args.evidence), repo_root=args.repo_root)
    print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
    if result["status"] == "pass":
        return 0
    if result["status"] == "blocked":
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
