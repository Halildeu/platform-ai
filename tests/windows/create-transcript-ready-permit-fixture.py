#!/usr/bin/env python3
"""Create an ephemeral signed transcript-ready permit for Windows contract tests.

This helper is test-only. It persists a raw ephemeral Ed25519 key under the CI
runner temp directory so one trust root can sign permit rotations. Production
permit signing remains a Vault Transit responsibility and never uses this file.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)

PAYLOAD_TYPE = (
    "application/vnd.acik.faz24.transcript-ready-pre-enable-verdict.v2+json"
)
KEY_ID = "vault-transit://meeting-ai/transcript-ready-permit-ci#v1"
GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
IMAGE_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def utc(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def pae(payload: bytes) -> bytes:
    payload_type = PAYLOAD_TYPE.encode("ascii")
    return (
        b"DSSEv1 "
        + str(len(payload_type)).encode("ascii")
        + b" "
        + payload_type
        + b" "
        + str(len(payload)).encode("ascii")
        + b" "
        + payload
    )


def write_private(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def load_or_create_key(path: Path) -> Ed25519PrivateKey:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        key = Ed25519PrivateKey.generate()
        raw = key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        write_private(path, raw)
        return key
    if len(raw) != 32:
        raise ValueError("test private key has an invalid size")
    return Ed25519PrivateKey.from_private_bytes(raw)


def ensure_trust_root(path: Path, key: Ed25519PrivateKey, now: dt.datetime) -> bytes:
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    if path.exists():
        raw = path.read_bytes()
        root = json.loads(raw)
        if (
            root.get("keyId") != KEY_ID
            or root.get("publicKeyBase64")
            != base64.b64encode(public).decode("ascii")
        ):
            raise ValueError("existing test trust root does not match the key")
        return raw
    root = {
        "schemaVersion": "faz24.transcriptReadyPermitTrustRoot.v1",
        "keyId": KEY_ID,
        "algorithm": "ed25519",
        "publicKeyBase64": base64.b64encode(public).decode("ascii"),
        "allowedAppEnvironments": ["stage", "test"],
        "notBefore": utc(now - dt.timedelta(hours=1)),
        "notAfter": utc(now + dt.timedelta(hours=2)),
    }
    raw = canonical(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return raw


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--envelope", required=True, type=Path)
    parser.add_argument("--trust-root", required=True, type=Path)
    parser.add_argument("--private-key", required=True, type=Path)
    parser.add_argument("--app-env", required=True, choices=("test", "stage"))
    parser.add_argument("--gitops-commit", required=True)
    parser.add_argument("--policy-sha256", required=True)
    parser.add_argument("--producer-digest", required=True)
    parser.add_argument("--backend-commit", required=True)
    parser.add_argument("--platform-ai-commit", required=True)
    parser.add_argument("--startup-sha256", required=True)
    parser.add_argument("--generated-at-offset-seconds", type=int, default=0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not GIT_SHA_RE.fullmatch(args.gitops_commit):
        raise ValueError("gitops commit is invalid")
    if not GIT_SHA_RE.fullmatch(args.backend_commit):
        raise ValueError("backend commit is invalid")
    if not GIT_SHA_RE.fullmatch(args.platform_ai_commit):
        raise ValueError("platform-ai commit is invalid")
    if not SHA_RE.fullmatch(args.policy_sha256):
        raise ValueError("policy sha256 is invalid")
    if not SHA_RE.fullmatch(args.startup_sha256):
        raise ValueError("startup sha256 is invalid")
    if not IMAGE_RE.fullmatch(args.producer_digest):
        raise ValueError("producer digest is invalid")

    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    generated_at = now + dt.timedelta(seconds=args.generated_at_offset_seconds)
    key = load_or_create_key(args.private_key)
    trust_raw = ensure_trust_root(args.trust_root, key, now)
    payload = {
        "schemaVersion": "faz24.transcriptReadyPreEnableVerdict.v2",
        "generatedAt": utc(generated_at),
        "issue": "platform-k8s-gitops#2610",
        "status": "accepted-candidate",
        "enableAuthorized": True,
        "checks": [
            {
                "name": "ci-contract",
                "passed": True,
                "message": "immutable producer imageID observed",
                "remediation": "",
            }
        ],
        "requiredRemediationEvidence": [],
        "binding": {
            "targetAppEnv": args.app_env,
            "expectedGitopsCommit": args.gitops_commit,
            "policySha256": args.policy_sha256,
            "producerCapability": {
                "transcriptImageDigest": args.producer_digest,
                "backendCommit": args.backend_commit,
            },
            "liveTranscriptPod": {
                "podUid": "de305d54-75b4-431b-adb2-eb6b9e546014",
                "imageDigest": args.producer_digest,
                "observedAt": utc(generated_at - dt.timedelta(seconds=1)),
                "evidenceSha256": "d" * 64,
            },
            "hostStartupGuard": {
                "platformAiCommit": args.platform_ai_commit,
                "startupScriptSha256": args.startup_sha256,
                "permitRequired": True,
            },
            "evidenceAgeSeconds": 1,
        },
        "boundary": (
            "Test-only candidate; production authority remains Vault Transit and "
            "the out-of-band pinned runtime trust root."
        ),
    }
    payload_raw = canonical(payload)
    envelope = {
        "payloadType": PAYLOAD_TYPE,
        "payload": base64.b64encode(payload_raw).decode("ascii"),
        "signatures": [
            {
                "keyid": KEY_ID,
                "sig": base64.b64encode(key.sign(pae(payload_raw))).decode("ascii"),
            }
        ],
    }
    args.envelope.parent.mkdir(parents=True, exist_ok=True)
    args.envelope.write_bytes(canonical(envelope))
    sys.stdout.write(hashlib.sha256(trust_raw).hexdigest() + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
