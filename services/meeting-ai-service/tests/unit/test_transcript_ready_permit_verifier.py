from __future__ import annotations

import base64
import copy
import datetime as dt
import hashlib
import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "deploy/gpu-host/verify-transcript-ready-permit.py"
GITOPS_COMMIT = "a" * 40
POLICY_SHA = "b" * 64
PRODUCER_DIGEST = "sha256:" + "c" * 64
AI_COMMIT = "d" * 40
STARTUP_SHA = "e" * 64
KEY_ID = "vault-transit://meeting-ai/transcript-ready-permit#v1"
NOW = dt.datetime(2026, 7, 20, 8, 0, 0, tzinfo=dt.UTC)


def load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("transcript_ready_permit_verifier", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


verifier = load_module()


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def pae(payload_type: str, payload: bytes) -> bytes:
    encoded_type = payload_type.encode("ascii")
    return (
        b"DSSEv1 "
        + str(len(encoded_type)).encode("ascii")
        + b" "
        + encoded_type
        + b" "
        + str(len(payload)).encode("ascii")
        + b" "
        + payload
    )


def documents() -> tuple[Ed25519PrivateKey, dict[str, Any], dict[str, Any]]:
    key = Ed25519PrivateKey.generate()
    public_key = key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    trust_root = {
        "schemaVersion": "faz24.transcriptReadyPermitTrustRoot.v1",
        "keyId": KEY_ID,
        "algorithm": "ed25519",
        "publicKeyBase64": base64.b64encode(public_key).decode("ascii"),
        "allowedAppEnvironments": ["test"],
        "notBefore": "2026-07-20T07:00:00Z",
        "notAfter": "2026-07-20T09:00:00Z",
    }
    payload = {
        "schemaVersion": "faz24.transcriptReadyPreEnableVerdict.v2",
        "generatedAt": "2026-07-20T07:59:00Z",
        "issue": "platform-k8s-gitops#2610",
        "status": "accepted-candidate",
        "enableAuthorized": True,
        "checks": [
            {
                "name": "live-runtime",
                "passed": True,
                "message": "immutable producer imageID observed",
                "remediation": "",
            }
        ],
        "requiredRemediationEvidence": [],
        "binding": {
            "targetAppEnv": "test",
            "expectedGitopsCommit": GITOPS_COMMIT,
            "policySha256": POLICY_SHA,
            "producerCapability": {
                "transcriptImageDigest": PRODUCER_DIGEST,
                "backendCommit": "f" * 40,
            },
            "liveTranscriptPod": {
                "podUid": "12345678-1234-4123-8123-123456789abc",
                "imageDigest": PRODUCER_DIGEST,
                "observedAt": "2026-07-20T07:58:30Z",
                "evidenceSha256": "1" * 64,
            },
            "hostStartupGuard": {
                "platformAiCommit": AI_COMMIT,
                "startupScriptSha256": STARTUP_SHA,
                "permitRequired": True,
            },
            "evidenceAgeSeconds": 30,
        },
        "boundary": (
            "A passing candidate is usable only by the allowlisted host startup guard; "
            "it is not an operator assertion or a production approval."
        ),
    }
    return key, trust_root, payload


def envelope(key: Ed25519PrivateKey, payload: dict[str, Any]) -> dict[str, Any]:
    payload_bytes = canonical(payload)
    payload_type = verifier.PAYLOAD_TYPE
    return {
        "payloadType": payload_type,
        "payload": base64.b64encode(payload_bytes).decode("ascii"),
        "signatures": [
            {
                "keyid": KEY_ID,
                "sig": base64.b64encode(key.sign(pae(payload_type, payload_bytes))).decode(
                    "ascii"
                ),
            }
        ],
    }


def write_documents(
    tmp_path: Path,
    trust_root: dict[str, Any],
    permit: dict[str, Any],
) -> tuple[Path, Path, str]:
    trust_path = tmp_path / "trust-root.json"
    envelope_path = tmp_path / "permit.dsse.json"
    trust_bytes = canonical(trust_root)
    trust_path.write_bytes(trust_bytes)
    envelope_path.write_bytes(canonical(permit))
    return trust_path, envelope_path, hashlib.sha256(trust_bytes).hexdigest()


def verify(
    tmp_path: Path,
    trust_root: dict[str, Any],
    permit: dict[str, Any],
    **overrides: Any,
) -> dict[str, Any]:
    trust_path, envelope_path, trust_sha = write_documents(tmp_path, trust_root, permit)
    arguments = {
        "envelope_path": envelope_path,
        "trust_root_path": trust_path,
        "expected_trust_root_sha256": trust_sha,
        "app_env": "test",
        "expected_gitops_commit": GITOPS_COMMIT,
        "expected_policy_sha256": POLICY_SHA,
        "expected_producer_image_digest": PRODUCER_DIGEST,
        "now": NOW,
    }
    arguments.update(overrides)
    return verifier.verify_permit(**arguments)


def test_valid_signed_permit_binds_live_candidate(tmp_path: Path) -> None:
    key, trust_root, payload = documents()

    verified = verify(tmp_path, trust_root, envelope(key, payload))

    assert verified == payload


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda value: value["binding"].update(targetAppEnv="stage"), "PERMIT_BINDING_MISMATCH"),
        (
            lambda value: value["binding"]["producerCapability"].update(
                transcriptImageDigest="sha256:" + "0" * 64
            ),
            "PERMIT_PRODUCER_DIGEST_MISMATCH",
        ),
        (lambda value: value.update(enableAuthorized=False), "PERMIT_VERDICT_REJECTED"),
    ],
)
def test_signed_but_wrong_candidate_is_rejected(
    tmp_path: Path,
    mutation: Any,
    code: str,
) -> None:
    key, trust_root, payload = documents()
    mutation(payload)

    with pytest.raises(verifier.PermitVerificationError, match=code):
        verify(tmp_path, trust_root, envelope(key, payload))


def test_payload_tamper_after_signing_is_rejected(tmp_path: Path) -> None:
    key, trust_root, payload = documents()
    signed = envelope(key, payload)
    tampered = copy.deepcopy(payload)
    tampered["binding"]["expectedGitopsCommit"] = "0" * 40
    signed["payload"] = base64.b64encode(canonical(tampered)).decode("ascii")

    with pytest.raises(verifier.PermitVerificationError, match="PERMIT_SIGNATURE_INVALID"):
        verify(tmp_path, trust_root, signed)


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (
            lambda value: value["binding"].update(evidenceAgeSeconds=29),
            "PERMIT_EVIDENCE_AGE_INVALID",
        ),
        (lambda value: value.update(boundary="  "), "PERMIT_BOUNDARY_INVALID"),
    ],
)
def test_signed_but_internally_inconsistent_evidence_is_rejected(
    tmp_path: Path,
    mutation: Any,
    code: str,
) -> None:
    key, trust_root, payload = documents()
    mutation(payload)

    with pytest.raises(verifier.PermitVerificationError, match=code):
        verify(tmp_path, trust_root, envelope(key, payload))


def test_unpinned_trust_root_is_rejected(tmp_path: Path) -> None:
    key, trust_root, payload = documents()
    trust_path, envelope_path, _trust_sha = write_documents(
        tmp_path, trust_root, envelope(key, payload)
    )

    with pytest.raises(verifier.PermitVerificationError, match="TRUST_ROOT_SHA256_MISMATCH"):
        verifier.verify_permit(
            envelope_path=envelope_path,
            trust_root_path=trust_path,
            expected_trust_root_sha256="0" * 64,
            app_env="test",
            expected_gitops_commit=GITOPS_COMMIT,
            expected_policy_sha256=POLICY_SHA,
            expected_producer_image_digest=PRODUCER_DIGEST,
            now=NOW,
        )


def test_wrong_signing_key_is_rejected(tmp_path: Path) -> None:
    _key, trust_root, payload = documents()
    attacker = Ed25519PrivateKey.generate()

    with pytest.raises(verifier.PermitVerificationError, match="PERMIT_SIGNATURE_INVALID"):
        verify(tmp_path, trust_root, envelope(attacker, payload))


def test_stale_permit_is_rejected_but_restart_can_reverify_signature(
    tmp_path: Path,
) -> None:
    key, trust_root, payload = documents()
    signed = envelope(key, payload)
    stale_now = NOW + dt.timedelta(minutes=20)

    with pytest.raises(verifier.PermitVerificationError, match="PERMIT_FRESHNESS_INVALID"):
        verify(tmp_path, trust_root, signed, now=stale_now)

    assert verify(
        tmp_path,
        trust_root,
        signed,
        now=stale_now,
        skip_freshness=True,
    ) == payload


def test_key_expiry_is_never_skipped_on_restart(tmp_path: Path) -> None:
    key, trust_root, payload = documents()

    with pytest.raises(verifier.PermitVerificationError, match="TRUST_ROOT_VALIDITY_INVALID"):
        verify(
            tmp_path,
            trust_root,
            envelope(key, payload),
            now=NOW + dt.timedelta(hours=2),
            skip_freshness=True,
        )
