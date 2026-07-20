"""Exercise the production SQLite stores with an operator-supplied test keyring file."""

from __future__ import annotations

import argparse
import base64
import json
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "services" / "meeting-ai-service"))

from app.services.durable_outbox import PayloadCipher, SqliteOutboxStore  # noqa: E402
from app.services.ready_event_inbox import (  # noqa: E402
    ReadyEventIdentity,
    SqliteReadyEventInbox,
)


def _stores(
    store_path: Path,
    keyring_path: Path,
) -> tuple[SqliteOutboxStore, SqliteReadyEventInbox]:
    material = json.loads(keyring_path.read_text(encoding="utf-8"))
    active_key_id = str(material["activeKeyId"])
    lookup_key_id = str(material["lookupKeyId"])
    raw_keys = {
        str(key_id): base64.b64decode(str(value), validate=True)
        for key_id, value in dict(material["keys"]).items()
    }
    lookup_key = raw_keys.pop(lookup_key_id)
    cipher = PayloadCipher(raw_keys, active_key_id, lookup_key=lookup_key)
    outbox = SqliteOutboxStore(store_path, cipher, max_rows=10)
    return outbox, SqliteReadyEventInbox(outbox, max_rows=20, max_failures=3)


def _identity() -> ReadyEventIdentity:
    return ReadyEventIdentity(
        event_key="meeting.transcript|session-rollback|meeting.transcript.ready|1",
        payload_sha256="a" * 64,
        tenant_id="tenant-rollback",
        meeting_id="meeting-rollback",
        session_id="session-rollback",
        finalization_version=1,
        analysis_run_id="analysis-ready-rollback",
    )


def _assert_active_key(store_path: Path, expected_key_id: str) -> None:
    with sqlite3.connect(store_path) as connection:
        outbox_keys = connection.execute(
            "SELECT DISTINCT key_id FROM analysis_delivery_outbox"
        ).fetchall()
        inbox_keys = connection.execute(
            "SELECT DISTINCT identity_key_id FROM meeting_transcript_ready_inbox"
        ).fetchall()
    expected = [(expected_key_id,)]
    if outbox_keys != expected or inbox_keys != expected:
        raise AssertionError(
            f"retained rows are not encrypted with {expected_key_id!r}: "
            f"outbox={outbox_keys!r}, inbox={inbox_keys!r}"
        )


def _assert_decryptable(
    store_path: Path,
    outbox: SqliteOutboxStore,
    inbox: SqliteReadyEventInbox,
) -> None:
    with outbox._connect() as connection:
        outbox_row = connection.execute(
            "SELECT analysis_run_id, meeting_id, key_id, nonce, ciphertext "
            "FROM analysis_delivery_outbox WHERE analysis_run_id = ?",
            ("analysis-outbox-rollback",),
        ).fetchone()
        inbox_row = connection.execute(
            "SELECT * FROM meeting_transcript_ready_inbox"
        ).fetchone()
    if outbox_row is None or inbox_row is None:
        raise AssertionError(f"retained rows are missing from {store_path}")
    payload = outbox._cipher.decrypt(
        key_id=str(outbox_row["key_id"]),
        nonce=bytes(outbox_row["nonce"]),
        ciphertext=bytes(outbox_row["ciphertext"]),
        analysis_run_id=str(outbox_row["analysis_run_id"]),
        meeting_id=str(outbox_row["meeting_id"]),
    )
    if payload != {"summary": "retained-rollback-proof"}:
        raise AssertionError("retained outbox payload could not be decrypted")
    if inbox._decrypt_identity(inbox_row) != _identity():
        raise AssertionError("retained ready-inbox identity could not be decrypted")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("seed", "verify"))
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--keyring", type=Path, required=True)
    parser.add_argument("--expected-active", required=True)
    args = parser.parse_args()

    outbox, inbox = _stores(args.store, args.keyring)
    if args.mode == "seed":
        outbox.enqueue(
            analysis_run_id="analysis-outbox-rollback",
            meeting_id="meeting-rollback",
            payload={"summary": "retained-rollback-proof"},
            now=1.0,
        )
        inbox.register_and_claim(_identity(), owner="seed", lease_sec=30.0, now=1.0)

    _assert_active_key(args.store, args.expected_active)
    _assert_decryptable(args.store, outbox, inbox)


if __name__ == "__main__":
    main()
