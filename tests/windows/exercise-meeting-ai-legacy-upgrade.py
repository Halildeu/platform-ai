"""Create and verify a real v1 outbox across Windows config upgrade."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sqlite3
import sys
from importlib.resources import files
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "services" / "meeting-ai-service"))

from app.services.durable_outbox import PayloadCipher, SqliteOutboxStore  # noqa: E402

RUN_ID = "44444444-4444-4444-8444-444444444444"
MEETING_ID = "11111111-1111-4111-8111-111111111111"
PAYLOAD = {
    "_canonical_tenant_id": "33333333-3333-4333-8333-333333333333",
    "analysis_spec_version": "meeting-intelligence-v1",
    "finalization_version": 1,
    "finalized_at": "2026-07-20T01:00:00Z",
    "meeting_id": MEETING_ID,
    "summary": "retained-v1-upgrade-proof",
    "transcript_session_id": "22222222-2222-4222-8222-222222222222",
    "transcript_sha256": "a" * 64,
}


def _material(path: Path) -> tuple[str, str | None, dict[str, bytes]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    keys = {
        str(key_id): base64.b64decode(str(value), validate=True)
        for key_id, value in dict(raw["keys"]).items()
    }
    return str(raw["activeKeyId"]), raw.get("lookupKeyId"), keys


def seed(store_path: Path, material_path: Path) -> None:
    active_key_id, _, keys = _material(material_path)
    plaintext = json.dumps(PAYLOAD, sort_keys=True, separators=(",", ":")).encode()
    nonce = bytes(range(12))
    aad = f"meeting-ai-outbox:v1:{RUN_ID}:{MEETING_ID}".encode()
    ciphertext = AESGCM(keys[active_key_id]).encrypt(nonce, plaintext, aad)
    migration = (
        files("app.migrations")
        .joinpath("0001_analysis_delivery_outbox.sql")
        .read_text(encoding="utf-8")
    )
    store_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(store_path) as connection:
        connection.executescript(migration)
        connection.execute(
            """
            INSERT INTO analysis_delivery_outbox (
                analysis_run_id, meeting_id, key_id, nonce, ciphertext,
                payload_sha256, state, attempt_count, next_attempt_at,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'PENDING', 0, 1, 1, 1)
            """,
            (
                RUN_ID,
                MEETING_ID,
                active_key_id,
                nonce,
                ciphertext,
                hashlib.sha256(plaintext).hexdigest(),
            ),
        )
        connection.execute("PRAGMA user_version=1")


def verify(store_path: Path, material_path: Path) -> None:
    active_key_id, lookup_key_id, keys = _material(material_path)
    if not isinstance(lookup_key_id, str):
        raise AssertionError("upgraded material is missing a lookup key id")
    lookup_key = keys.pop(lookup_key_id)
    store = SqliteOutboxStore(
        store_path,
        PayloadCipher(keys, active_key_id, lookup_key=lookup_key),
        max_rows=10,
    )
    store.assert_delivery_capability_compatible(
        required_payload_fields=frozenset(PAYLOAD) - {"summary"}
    )
    message = store.claim_next(owner="upgrade-proof", lease_sec=30.0, now=2.0)
    if message is None or message.payload != PAYLOAD:
        raise AssertionError("retained v1 outbox payload was not preserved")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("seed", "verify"))
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--material", type=Path, required=True)
    args = parser.parse_args()
    if args.mode == "seed":
        seed(args.store, args.material)
    else:
        verify(args.store, args.material)


if __name__ == "__main__":
    main()
