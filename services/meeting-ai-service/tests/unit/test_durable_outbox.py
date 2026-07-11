"""Durability, encryption, and lease tests for the embedded analysis outbox."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.services.durable_outbox import (
    OutboxConflictError,
    OutboxFullError,
    OutboxIntegrityError,
    OutboxKeyUnavailableError,
    PayloadCipher,
    SqliteOutboxStore,
)

KEY_V1 = b"1" * 32
KEY_V2 = b"2" * 32


def _store(
    path: Path,
    *,
    keys: dict[str, bytes] | None = None,
    active: str = "v1",
    max_rows: int = 10,
) -> SqliteOutboxStore:
    return SqliteOutboxStore(
        path,
        PayloadCipher(keys or {"v1": KEY_V1}, active),
        max_rows=max_rows,
    )


def test_payload_is_encrypted_at_rest_and_deleted_after_delivery(tmp_path: Path) -> None:
    path = tmp_path / "outbox.sqlite3"
    store = _store(path)
    payload = {"summary": "GIZLI-TOPLANTI-OZETI", "decisions": ["Karar"]}

    assert store.enqueue(
        analysis_run_id="run-1",
        meeting_id="meeting-1",
        payload=payload,
        now=10.0,
    )
    wal_path = Path(f"{path}-wal")
    database_bytes = path.read_bytes() + (wal_path.read_bytes() if wal_path.exists() else b"")
    assert b"GIZLI-TOPLANTI-OZETI" not in database_bytes

    claimed = store.claim_next(owner="worker-a", lease_sec=30.0, now=10.0)
    assert claimed is not None
    assert claimed.payload == payload
    assert claimed.attempt_count == 1
    assert store.mark_delivered(analysis_run_id="run-1", owner="worker-a")
    assert store.summary(now=11.0).pending == 0


def test_enqueue_is_idempotent_but_rejects_same_id_with_different_payload(tmp_path: Path) -> None:
    store = _store(tmp_path / "outbox.sqlite3")
    assert store.enqueue(analysis_run_id="run-1", meeting_id="meeting-1", payload={"summary": "A"})
    assert not store.enqueue(
        analysis_run_id="run-1", meeting_id="meeting-1", payload={"summary": "A"}
    )
    with pytest.raises(OutboxConflictError):
        store.enqueue(analysis_run_id="run-1", meeting_id="meeting-1", payload={"summary": "B"})
    with pytest.raises(OutboxConflictError):
        store.enqueue(analysis_run_id="run-1", meeting_id="meeting-2", payload={"summary": "A"})


def test_queue_bound_is_checked_in_the_write_transaction(tmp_path: Path) -> None:
    store = _store(tmp_path / "outbox.sqlite3", max_rows=1)
    store.enqueue(analysis_run_id="run-1", meeting_id="meeting-1", payload={"x": 1})
    with pytest.raises(OutboxFullError):
        store.enqueue(analysis_run_id="run-2", meeting_id="meeting-1", payload={"x": 2})


def test_lease_is_exclusive_and_expired_work_is_reclaimed(tmp_path: Path) -> None:
    store = _store(tmp_path / "outbox.sqlite3")
    store.enqueue(analysis_run_id="run-1", meeting_id="meeting-1", payload={"x": 1}, now=100.0)
    first = store.claim_next(owner="worker-a", lease_sec=10.0, now=100.0)
    assert first is not None
    assert store.claim_next(owner="worker-b", lease_sec=10.0, now=105.0) is None

    reclaimed = store.claim_next(owner="worker-b", lease_sec=10.0, now=111.0)
    assert reclaimed is not None
    assert reclaimed.analysis_run_id == "run-1"
    assert reclaimed.attempt_count == 2
    assert not store.mark_delivered(analysis_run_id="run-1", owner="worker-a")
    assert store.mark_delivered(analysis_run_id="run-1", owner="worker-b")


def test_key_rotation_reads_old_rows_and_refuses_premature_key_removal(tmp_path: Path) -> None:
    path = tmp_path / "outbox.sqlite3"
    _store(path).enqueue(analysis_run_id="run-1", meeting_id="meeting-1", payload={"summary": "A"})
    rotated = _store(path, keys={"v1": KEY_V1, "v2": KEY_V2}, active="v2")
    rotated.enqueue(analysis_run_id="run-2", meeting_id="meeting-1", payload={"summary": "B"})
    assert rotated.claim_next(owner="worker", lease_sec=30.0) is not None

    with pytest.raises(OutboxKeyUnavailableError):
        _store(path, keys={"v2": KEY_V2}, active="v2")


def test_cipher_rejects_non_aes256_keys() -> None:
    with pytest.raises(OutboxKeyUnavailableError, match="exactly 32 bytes"):
        PayloadCipher({"v1": b"too-short"}, "v1")


def test_ciphertext_tamper_moves_row_to_dead_letter(tmp_path: Path) -> None:
    path = tmp_path / "outbox.sqlite3"
    store = _store(path)
    store.enqueue(
        analysis_run_id="run-1", meeting_id="meeting-1", payload={"summary": "A"}, now=5.0
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE analysis_delivery_outbox SET ciphertext = X'00' WHERE analysis_run_id = 'run-1'"
        )

    with pytest.raises(OutboxIntegrityError):
        store.claim_next(owner="worker", lease_sec=30.0, now=5.0)
    assert store.summary(now=6.0).dead == 1


def test_dead_letter_can_be_explicitly_requeued(tmp_path: Path) -> None:
    store = _store(tmp_path / "outbox.sqlite3")
    store.enqueue(analysis_run_id="run-1", meeting_id="meeting-1", payload={"x": 1})
    assert store.claim_next(owner="worker", lease_sec=30.0) is not None
    assert store.mark_dead(analysis_run_id="run-1", owner="worker", error_code="http_400")
    assert store.requeue_dead("run-1")
    summary = store.summary()
    assert summary.pending == 1
    assert summary.dead == 0


def test_dead_letter_metadata_is_bounded_and_payload_free(tmp_path: Path) -> None:
    store = _store(tmp_path / "outbox.sqlite3")
    store.enqueue(
        analysis_run_id="run-1",
        meeting_id="meeting-1",
        payload={"summary": "must-not-be-exposed"},
        now=10.0,
    )
    assert store.claim_next(owner="worker", lease_sec=30.0, now=10.0) is not None
    assert store.mark_dead(
        analysis_run_id="run-1",
        owner="worker",
        error_code="ingestion_http_400",
        now=11.0,
    )

    rows = store.list_dead(limit=1)
    assert len(rows) == 1
    assert rows[0].analysis_run_id == "run-1"
    assert rows[0].attempt_count == 1
    assert rows[0].last_error_code == "ingestion_http_400"
    assert "must-not-be-exposed" not in repr(rows[0])
    with pytest.raises(ValueError):
        store.list_dead(limit=0)
