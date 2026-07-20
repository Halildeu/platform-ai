#!/usr/bin/env python3
"""Verify a Vault Transit signed Faz 24 transcript-ready activation permit.

The verifier is intentionally standalone so the Windows PowerShell 5.1 host
guard can use the meeting-ai Python runtime for Ed25519 verification. Private
key material is never accepted; the only trust input is an out-of-band pinned
public trust-root document.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import datetime as dt
import hashlib
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, NoReturn

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

ENVELOPE_FIELDS = frozenset({"payloadType", "payload", "signatures"})
SIGNATURE_FIELDS = frozenset({"keyid", "sig"})
TRUST_ROOT_FIELDS = frozenset(
    {
        "schemaVersion",
        "keyId",
        "algorithm",
        "publicKeyBase64",
        "allowedAppEnvironments",
        "notBefore",
        "notAfter",
    }
)
PAYLOAD_FIELDS = frozenset(
    {
        "schemaVersion",
        "generatedAt",
        "issue",
        "status",
        "enableAuthorized",
        "checks",
        "requiredRemediationEvidence",
        "binding",
        "boundary",
    }
)
CHECK_FIELDS = frozenset({"name", "passed", "message", "remediation"})
BINDING_FIELDS = frozenset(
    {
        "targetAppEnv",
        "expectedGitopsCommit",
        "policySha256",
        "producerCapability",
        "liveTranscriptPod",
        "hostStartupGuard",
        "evidenceAgeSeconds",
    }
)
PRODUCER_FIELDS = frozenset({"transcriptImageDigest", "backendCommit"})
LIVE_POD_FIELDS = frozenset(
    {"podUid", "imageDigest", "observedAt", "evidenceSha256"}
)
HOST_GUARD_FIELDS = frozenset(
    {"platformAiCommit", "startupScriptSha256", "permitRequired"}
)
TRUST_ROOT_SCHEMA = "faz24.transcriptReadyPermitTrustRoot.v1"
PAYLOAD_SCHEMA = "faz24.transcriptReadyPreEnableVerdict.v2"
PAYLOAD_TYPE = "application/vnd.acik.faz24.transcript-ready-pre-enable-verdict.v2+json"
ISSUE = "platform-k8s-gitops#2610"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IMAGE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
KEY_ID_RE = re.compile(r"^vault-transit://[a-z0-9][a-z0-9-]*/[A-Za-z0-9_.-]+#v[1-9][0-9]*$")
UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
MAX_DOCUMENT_BYTES = 1024 * 1024


class PermitVerificationError(RuntimeError):
    """A fail-closed permit or trust-root contract violation."""


def _reject(code: str) -> NoReturn:
    raise PermitVerificationError(code)


def _mapping(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            _reject("JSON_DUPLICATE_KEY")
        value[key] = item
    return value


def _load_json_bytes(raw: bytes, label: str) -> dict[str, Any]:
    if not raw or len(raw) > MAX_DOCUMENT_BYTES or b"\x00" in raw:
        _reject(f"{label}_SIZE_INVALID")
    if raw.startswith(b"\xef\xbb\xbf"):
        _reject(f"{label}_BOM_FORBIDDEN")
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_mapping,
            parse_float=lambda _value: _reject("JSON_FLOAT_FORBIDDEN"),
            parse_constant=lambda _value: _reject("JSON_CONSTANT_FORBIDDEN"),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PermitVerificationError(f"{label}_JSON_INVALID") from exc
    if not isinstance(value, dict):
        _reject(f"{label}_ROOT_INVALID")
    return value


def _load_json(path: Path, label: str) -> tuple[bytes, dict[str, Any]]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PermitVerificationError(f"{label}_UNAVAILABLE") from exc
    return raw, _load_json_bytes(raw, label)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _strict_b64(value: Any, code: str) -> bytes:
    if not isinstance(value, str):
        _reject(code)
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise PermitVerificationError(code) from exc
    if base64.b64encode(decoded).decode("ascii") != value:
        _reject(code)
    return decoded


def _utc(value: Any, code: str) -> dt.datetime:
    if not isinstance(value, str) or not UTC_RE.fullmatch(value):
        _reject(code)
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.UTC
        )
    except ValueError as exc:
        raise PermitVerificationError(code) from exc


def _pae(payload_type: str, payload: bytes) -> bytes:
    payload_type_bytes = payload_type.encode("ascii")
    return (
        b"DSSEv1 "
        + str(len(payload_type_bytes)).encode("ascii")
        + b" "
        + payload_type_bytes
        + b" "
        + str(len(payload)).encode("ascii")
        + b" "
        + payload
    )


def _validate_trust_root(
    raw: bytes,
    root: dict[str, Any],
    *,
    expected_sha256: str,
    app_env: str,
    now: dt.datetime,
) -> tuple[str, bytes, dt.datetime, dt.datetime]:
    if not SHA256_RE.fullmatch(expected_sha256):
        _reject("TRUST_ROOT_EXPECTED_SHA256_INVALID")
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        _reject("TRUST_ROOT_SHA256_MISMATCH")
    if set(root) != TRUST_ROOT_FIELDS:
        _reject("TRUST_ROOT_FIELDS_INVALID")
    if root.get("schemaVersion") != TRUST_ROOT_SCHEMA:
        _reject("TRUST_ROOT_SCHEMA_INVALID")
    key_id = root.get("keyId")
    if not isinstance(key_id, str) or not KEY_ID_RE.fullmatch(key_id):
        _reject("TRUST_ROOT_KEY_ID_INVALID")
    if root.get("algorithm") != "ed25519":
        _reject("TRUST_ROOT_ALGORITHM_INVALID")
    environments = root.get("allowedAppEnvironments")
    if (
        not isinstance(environments, list)
        or not environments
        or environments != sorted(set(environments))
        or any(value not in {"test", "stage", "prod"} for value in environments)
        or app_env not in environments
    ):
        _reject("TRUST_ROOT_ENVIRONMENT_INVALID")
    not_before = _utc(root.get("notBefore"), "TRUST_ROOT_NOT_BEFORE_INVALID")
    not_after = _utc(root.get("notAfter"), "TRUST_ROOT_NOT_AFTER_INVALID")
    if not not_before < not_after or not not_before <= now <= not_after:
        _reject("TRUST_ROOT_VALIDITY_INVALID")
    public_key = _strict_b64(root.get("publicKeyBase64"), "TRUST_ROOT_KEY_INVALID")
    if len(public_key) != 32:
        _reject("TRUST_ROOT_KEY_INVALID")
    return key_id, public_key, not_before, not_after


def _validate_payload(
    payload: dict[str, Any],
    *,
    app_env: str,
    expected_gitops_commit: str,
    expected_policy_sha256: str,
    expected_producer_image_digest: str,
    now: dt.datetime,
    max_age_seconds: int,
    skip_freshness: bool,
    trust_not_before: dt.datetime,
    trust_not_after: dt.datetime,
) -> None:
    if set(payload) != PAYLOAD_FIELDS:
        _reject("PERMIT_PAYLOAD_FIELDS_INVALID")
    if (
        payload.get("schemaVersion") != PAYLOAD_SCHEMA
        or payload.get("issue") != ISSUE
        or payload.get("status") != "accepted-candidate"
        or payload.get("enableAuthorized") is not True
    ):
        _reject("PERMIT_VERDICT_REJECTED")
    generated_at = _utc(payload.get("generatedAt"), "PERMIT_GENERATED_AT_INVALID")
    if not trust_not_before <= generated_at <= trust_not_after:
        _reject("PERMIT_OUTSIDE_KEY_VALIDITY")
    if not skip_freshness:
        age_seconds = (now - generated_at).total_seconds()
        if age_seconds < -30 or age_seconds > max_age_seconds:
            _reject("PERMIT_FRESHNESS_INVALID")
    boundary = payload.get("boundary")
    if (
        not isinstance(boundary, str)
        or not boundary.strip()
        or len(boundary) > 2048
    ):
        _reject("PERMIT_BOUNDARY_INVALID")

    checks = payload.get("checks")
    if (
        not isinstance(checks, list)
        or not checks
        or any(
            not isinstance(check, dict)
            or set(check) != CHECK_FIELDS
            or not isinstance(check.get("name"), str)
            or not check.get("name")
            or check.get("passed") is not True
            or not isinstance(check.get("message"), str)
            or not isinstance(check.get("remediation"), str)
            for check in checks
        )
        or payload.get("requiredRemediationEvidence") != []
    ):
        _reject("PERMIT_CHECKS_REJECTED")

    binding = payload.get("binding")
    if not isinstance(binding, dict) or set(binding) != BINDING_FIELDS:
        _reject("PERMIT_BINDING_INVALID")
    if (
        binding.get("targetAppEnv") != app_env
        or binding.get("expectedGitopsCommit") != expected_gitops_commit
        or binding.get("policySha256") != expected_policy_sha256
    ):
        _reject("PERMIT_BINDING_MISMATCH")
    producer = binding.get("producerCapability")
    if (
        not isinstance(producer, dict)
        or set(producer) != PRODUCER_FIELDS
        or producer.get("transcriptImageDigest") != expected_producer_image_digest
        or not isinstance(producer.get("backendCommit"), str)
        or not GIT_SHA_RE.fullmatch(producer["backendCommit"])
    ):
        _reject("PERMIT_PRODUCER_DIGEST_MISMATCH")
    live_pod = binding.get("liveTranscriptPod")
    if not isinstance(live_pod, dict) or set(live_pod) != LIVE_POD_FIELDS:
        _reject("PERMIT_LIVE_RUNTIME_INVALID")
    observed_at = _utc(live_pod.get("observedAt"), "PERMIT_LIVE_RUNTIME_INVALID")
    if (
        not isinstance(live_pod.get("podUid"), str)
        or not UUID_RE.fullmatch(live_pod["podUid"])
        or live_pod.get("imageDigest") != expected_producer_image_digest
        or not isinstance(live_pod.get("evidenceSha256"), str)
        or not SHA256_RE.fullmatch(live_pod["evidenceSha256"])
        or observed_at > generated_at
        or generated_at - observed_at > dt.timedelta(seconds=900)
    ):
        _reject("PERMIT_LIVE_RUNTIME_INVALID")
    host_guard = binding.get("hostStartupGuard")
    if (
        not isinstance(host_guard, dict)
        or set(host_guard) != HOST_GUARD_FIELDS
        or host_guard.get("permitRequired") is not True
        or not isinstance(host_guard.get("platformAiCommit"), str)
        or not GIT_SHA_RE.fullmatch(host_guard["platformAiCommit"])
        or not isinstance(host_guard.get("startupScriptSha256"), str)
        or not SHA256_RE.fullmatch(host_guard["startupScriptSha256"])
    ):
        _reject("PERMIT_HOST_GUARD_INVALID")
    evidence_age = binding.get("evidenceAgeSeconds")
    observed_age = int((generated_at - observed_at).total_seconds())
    if (
        not isinstance(evidence_age, int)
        or isinstance(evidence_age, bool)
        or evidence_age < 0
        or evidence_age > 900
        or evidence_age != observed_age
    ):
        _reject("PERMIT_EVIDENCE_AGE_INVALID")


def verify_permit(
    *,
    envelope_path: Path,
    trust_root_path: Path,
    expected_trust_root_sha256: str,
    app_env: str,
    expected_gitops_commit: str,
    expected_policy_sha256: str,
    expected_producer_image_digest: str,
    now: dt.datetime,
    max_age_seconds: int = 900,
    skip_freshness: bool = False,
) -> dict[str, Any]:
    if app_env not in {"test", "stage", "prod"}:
        _reject("APP_ENV_INVALID")
    if not GIT_SHA_RE.fullmatch(expected_gitops_commit):
        _reject("EXPECTED_GITOPS_COMMIT_INVALID")
    if not SHA256_RE.fullmatch(expected_policy_sha256):
        _reject("EXPECTED_POLICY_SHA256_INVALID")
    if not IMAGE_DIGEST_RE.fullmatch(expected_producer_image_digest):
        _reject("EXPECTED_PRODUCER_DIGEST_INVALID")
    if not 60 <= max_age_seconds <= 3600:
        _reject("MAX_AGE_INVALID")
    if now.tzinfo is None or now.utcoffset() != dt.timedelta(0):
        _reject("NOW_MUST_BE_UTC")

    trust_raw, trust_root = _load_json(trust_root_path, "TRUST_ROOT")
    key_id, public_key, not_before, not_after = _validate_trust_root(
        trust_raw,
        trust_root,
        expected_sha256=expected_trust_root_sha256,
        app_env=app_env,
        now=now,
    )
    _envelope_raw, envelope = _load_json(envelope_path, "PERMIT_ENVELOPE")
    if set(envelope) != ENVELOPE_FIELDS or envelope.get("payloadType") != PAYLOAD_TYPE:
        _reject("PERMIT_ENVELOPE_FIELDS_INVALID")
    payload_bytes = _strict_b64(envelope.get("payload"), "PERMIT_PAYLOAD_BASE64_INVALID")
    payload = _load_json_bytes(payload_bytes, "PERMIT_PAYLOAD")
    if payload_bytes != _canonical_json(payload):
        _reject("PERMIT_PAYLOAD_NON_CANONICAL")
    signatures = envelope.get("signatures")
    if not isinstance(signatures, list) or len(signatures) != 1:
        _reject("PERMIT_SIGNATURE_COUNT_INVALID")
    signature_entry = signatures[0]
    if (
        not isinstance(signature_entry, dict)
        or set(signature_entry) != SIGNATURE_FIELDS
        or signature_entry.get("keyid") != key_id
    ):
        _reject("PERMIT_SIGNATURE_KEY_INVALID")
    signature = _strict_b64(signature_entry.get("sig"), "PERMIT_SIGNATURE_INVALID")
    if len(signature) != 64:
        _reject("PERMIT_SIGNATURE_INVALID")
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            signature, _pae(PAYLOAD_TYPE, payload_bytes)
        )
    except (InvalidSignature, ValueError) as exc:
        raise PermitVerificationError("PERMIT_SIGNATURE_INVALID") from exc

    _validate_payload(
        payload,
        app_env=app_env,
        expected_gitops_commit=expected_gitops_commit,
        expected_policy_sha256=expected_policy_sha256,
        expected_producer_image_digest=expected_producer_image_digest,
        now=now,
        max_age_seconds=max_age_seconds,
        skip_freshness=skip_freshness,
        trust_not_before=not_before,
        trust_not_after=not_after,
    )
    return payload


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--envelope", required=True, type=Path)
    parser.add_argument("--trust-root", required=True, type=Path)
    parser.add_argument("--expected-trust-root-sha256", required=True)
    parser.add_argument("--app-env", required=True, choices=("test", "stage", "prod"))
    parser.add_argument("--expected-gitops-commit", required=True)
    parser.add_argument("--expected-policy-sha256", required=True)
    parser.add_argument("--expected-producer-image-digest", required=True)
    parser.add_argument("--max-age-seconds", type=int, default=900)
    parser.add_argument("--skip-freshness", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        payload = verify_permit(
            envelope_path=args.envelope,
            trust_root_path=args.trust_root,
            expected_trust_root_sha256=args.expected_trust_root_sha256,
            app_env=args.app_env,
            expected_gitops_commit=args.expected_gitops_commit,
            expected_policy_sha256=args.expected_policy_sha256,
            expected_producer_image_digest=args.expected_producer_image_digest,
            now=dt.datetime.now(dt.UTC),
            max_age_seconds=args.max_age_seconds,
            skip_freshness=args.skip_freshness,
        )
    except PermitVerificationError as exc:
        sys.stderr.write(f"permit verification rejected: {exc}\n")
        return 2
    sys.stdout.buffer.write(_canonical_json(payload) + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
