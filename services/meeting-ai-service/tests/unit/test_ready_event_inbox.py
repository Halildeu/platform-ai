"""SQLite ready inbox idempotency, lease, and atomic-outbox tests."""

from __future__ import annotations

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from importlib.resources import files
from pathlib import Path

import pytest

from app.services.durable_outbox import (
    OutboxConflictError,
    OutboxError,
    OutboxKeyUnavailableError,
    PayloadCipher,
    SqliteOutboxStore,
)
from app.services.ready_event_inbox import (
    ReadyClaimDisposition,
    ReadyDeadReason,
    ReadyEventIdentity,
    ReadyInboxLeaseLostError,
    ReadyInboxState,
    SqliteReadyEventInbox,
)

KEY = b"K" * 32
HASH_A = "a" * 64
HASH_B = "b" * 64


def _lookup_digest(lookup_key: str) -> str:
    return PayloadCipher({"v1": KEY}, "v1").lookup_digests(
        purpose="ready-event-inbox",
        value=lookup_key,
    )[0]


def _stores(path: Path) -> tuple[SqliteOutboxStore, SqliteReadyEventInbox]:
    outbox = SqliteOutboxStore(
        path,
        PayloadCipher({"v1": KEY}, "v1"),
        max_rows=10,
    )
    return outbox, SqliteReadyEventInbox(outbox, max_rows=20, max_failures=3)


def _identity(
    *,
    payload_hash: str = HASH_A,
    run_id: str = "run-1",
    tenant_id: str = "33333333-3333-4333-8333-333333333333",
) -> ReadyEventIdentity:
    return ReadyEventIdentity(
        event_key=(
            "meeting.transcript|22222222-2222-4222-8222-222222222222" "|meeting.transcript.ready|1"
        ),
        payload_sha256=payload_hash,
        tenant_id=tenant_id,
        meeting_id="11111111-1111-4111-8111-111111111111",
        session_id="22222222-2222-4222-8222-222222222222",
        finalization_version=1,
        analysis_run_id=run_id,
    )


def _create_version_3_inbox(path: Path, identity: ReadyEventIdentity) -> None:
    with sqlite3.connect(path) as connection:
        for version, name in (
            (1, "0001_analysis_delivery_outbox.sql"),
            (2, "0002_ready_event_inbox.sql"),
            (3, "0003_ready_event_redrive.sql"),
        ):
            sql = files("app.migrations").joinpath(name).read_text(encoding="utf-8")
            connection.executescript(f"{sql}\nPRAGMA user_version={version};")
        connection.execute(
            """
            INSERT INTO meeting_transcript_ready_inbox (
                event_key, payload_sha256, state, tenant_id, meeting_id,
                session_id, finalization_version, analysis_run_id,
                next_attempt_at, created_at, updated_at
            ) VALUES (?, ?, 'RECEIVED', ?, ?, ?, ?, ?, 1, 1, 1)
            """,
            (
                identity.event_key,
                identity.payload_sha256,
                identity.tenant_id,
                identity.meeting_id,
                identity.session_id,
                identity.finalization_version,
                identity.analysis_run_id,
            ),
        )


def _upgrade_to_legacy_version_4(path: Path) -> None:
    cipher = PayloadCipher({"v1": KEY}, "v1")
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM meeting_transcript_ready_inbox").fetchone()
        assert row is not None
        sql = (
            files("app.migrations")
            .joinpath("0004_ready_event_inbox_encryption.sql")
            .read_text(encoding="utf-8")
        )
        connection.execute(sql.strip().rstrip(";"))
        envelope: dict[str, object] = {
            "eventKey": row["event_key"],
            "payloadSha256": row["payload_sha256"],
            "tenantId": row["tenant_id"],
            "meetingId": row["meeting_id"],
            "sessionId": row["session_id"],
            "finalizationVersion": row["finalization_version"],
            "analysisRunId": row["analysis_run_id"],
        }
        key_id, digest, nonce, ciphertext = cipher.encrypt_metadata(
            purpose="ready-event-inbox",
            lookup_value=str(row["event_key"]),
            payload=envelope,
        )
        connection.execute(
            """
            INSERT INTO meeting_transcript_ready_inbox_v4 (
                event_key_digest, identity_key_id, identity_nonce,
                identity_ciphertext, state, failure_count,
                lease_recovery_count, next_attempt_at, lease_owner,
                lease_until, created_at, updated_at, last_error_code,
                dlq_published_at, dead_reason, redrive_count,
                last_redriven_at, last_redrive_reference
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                digest,
                key_id,
                nonce,
                ciphertext,
                row["state"],
                row["failure_count"],
                row["lease_recovery_count"],
                row["next_attempt_at"],
                row["lease_owner"],
                row["lease_until"],
                row["created_at"],
                row["updated_at"],
                row["last_error_code"],
                row["dlq_published_at"],
                row["dead_reason"],
                row["redrive_count"],
                row["last_redriven_at"],
                row["last_redrive_reference"],
            ),
        )
        connection.execute("DROP TABLE meeting_transcript_ready_inbox")
        connection.execute(
            "ALTER TABLE meeting_transcript_ready_inbox_v4 "
            "RENAME TO meeting_transcript_ready_inbox"
        )
        connection.execute(
            """
            CREATE INDEX idx_meeting_transcript_ready_due
            ON meeting_transcript_ready_inbox(
                state, next_attempt_at, lease_until, created_at
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX idx_meeting_transcript_ready_terminal
            ON meeting_transcript_ready_inbox(state, updated_at)
            """
        )
        connection.execute(
            """
            CREATE TABLE meeting_ai_store_metadata (
                name TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO meeting_ai_store_metadata(name, value) VALUES (?, 'complete')",
            (SqliteOutboxStore._INBOX_SCRUB_MARKER,),
        )
        connection.execute("PRAGMA user_version=4")


def test_migration_upgrades_store_to_lease_fenced_version_5(tmp_path: Path) -> None:
    path = tmp_path / "delivery.sqlite3"
    _stores(path)

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(meeting_transcript_ready_inbox)"
            ).fetchall()
        }
    assert {
        "event_key_digest",
        "identity_key_id",
        "identity_nonce",
        "identity_ciphertext",
        "lease_recovery_count",
        "dead_reason",
        "redrive_count",
        "lease_token",
    } <= columns
    assert {"event_key", "payload_sha256", "tenant_id", "analysis_run_id"}.isdisjoint(columns)


def test_version_1_outbox_rows_survive_in_place_upgrade(tmp_path: Path) -> None:
    path = tmp_path / "delivery.sqlite3"
    old, _ = _stores(path)
    old.enqueue(
        analysis_run_id="old-run",
        meeting_id="old-meeting",
        payload={"summary": "encrypted-old-row"},
        now=1.0,
    )
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE meeting_transcript_ready_inbox")
        connection.execute("PRAGMA user_version=1")

    upgraded, _ = _stores(path)
    claimed = upgraded.claim_next(owner="worker", lease_sec=30.0, now=2.0)
    assert claimed is not None
    assert claimed.analysis_run_id == "old-run"
    assert claimed.payload == {"summary": "encrypted-old-row"}


def test_version_3_plaintext_identity_is_encrypted_and_scrubbed_on_upgrade(
    tmp_path: Path,
) -> None:
    path = tmp_path / "delivery.sqlite3"
    identity = _identity()
    _create_version_3_inbox(path, identity)

    _, inbox = _stores(path)
    claim = inbox.register_and_claim(identity, owner="worker", lease_sec=30.0, now=2.0)
    assert claim.disposition is ReadyClaimDisposition.CLAIMED
    durable_bytes = b"".join(
        candidate.read_bytes()
        for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm"))
        if candidate.exists()
    )
    for plaintext in (
        identity.event_key,
        identity.payload_sha256,
        identity.tenant_id,
        identity.meeting_id,
        identity.session_id,
        identity.analysis_run_id,
    ):
        assert plaintext.encode() not in durable_bytes


def test_version_4_event_only_lookup_is_tenant_bound_on_upgrade(tmp_path: Path) -> None:
    path = tmp_path / "delivery.sqlite3"
    identity = _identity()
    _create_version_3_inbox(path, identity)
    _upgrade_to_legacy_version_4(path)
    legacy_digest = _lookup_digest(identity.event_key)

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert connection.execute(
            "SELECT event_key_digest FROM meeting_transcript_ready_inbox"
        ).fetchone() == (legacy_digest,)

    _, inbox = _stores(path)
    claim = inbox.register_and_claim(identity, owner="worker", lease_sec=30.0, now=2.0)
    assert claim.disposition is ReadyClaimDisposition.CLAIMED
    assert claim.lease_token is not None
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        rows = connection.execute(
            "SELECT event_key_digest, lease_token FROM meeting_transcript_ready_inbox"
        ).fetchall()
    assert rows == [(_lookup_digest(identity.lookup_key), claim.lease_token)]


def test_claim_preserves_stored_run_id_across_identity_algorithm_upgrade(
    tmp_path: Path,
) -> None:
    path = tmp_path / "delivery.sqlite3"
    legacy_identity = _identity(run_id="legacy-run-id")
    _create_version_3_inbox(path, legacy_identity)
    _upgrade_to_legacy_version_4(path)

    _, inbox = _stores(path)
    upgraded_identity = _identity(run_id="tenant-bound-run-id")
    claim = inbox.register_and_claim(
        upgraded_identity,
        owner="worker",
        lease_sec=30.0,
        now=2.0,
    )

    assert claim.disposition is ReadyClaimDisposition.CLAIMED
    assert claim.analysis_run_id == "legacy-run-id"


def test_pending_plaintext_scrub_resumes_after_startup_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "delivery.sqlite3"
    identity = _identity()
    _create_version_3_inbox(path, identity)
    complete_scrub = SqliteOutboxStore._complete_inbox_plaintext_scrub

    def interrupt_scrub(
        _store: SqliteOutboxStore,
        _connection: sqlite3.Connection,
    ) -> None:
        raise OutboxError("simulated process interruption after migration commit")

    monkeypatch.setattr(SqliteOutboxStore, "_complete_inbox_plaintext_scrub", interrupt_scrub)
    with pytest.raises(OutboxError, match="simulated process interruption"):
        _stores(path)

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert connection.execute(
            "SELECT value FROM meeting_ai_store_metadata WHERE name = ?",
            (SqliteOutboxStore._INBOX_SCRUB_MARKER,),
        ).fetchone() == ("pending",)

    monkeypatch.setattr(SqliteOutboxStore, "_complete_inbox_plaintext_scrub", complete_scrub)
    _, inbox = _stores(path)
    claim = inbox.register_and_claim(identity, owner="worker", lease_sec=30.0, now=2.0)
    assert claim.disposition is ReadyClaimDisposition.CLAIMED
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT value FROM meeting_ai_store_metadata WHERE name = ?",
            (SqliteOutboxStore._INBOX_SCRUB_MARKER,),
        ).fetchone() == ("complete",)

    durable_bytes = b"".join(
        candidate.read_bytes()
        for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm"))
        if candidate.exists()
    )
    assert identity.event_key.encode() not in durable_bytes


def test_busy_wal_checkpoint_keeps_plaintext_scrub_pending() -> None:
    class BusyCheckpoint:
        def execute(self, _statement: str) -> BusyCheckpoint:
            return self

        @staticmethod
        def fetchone() -> tuple[int, int, int]:
            return (1, 1, 0)

    with pytest.raises(OutboxError, match="WAL truncation is busy"):
        SqliteOutboxStore._checkpoint_truncate(BusyCheckpoint())  # type: ignore[arg-type]


def test_inbox_identity_is_encrypted_at_rest(tmp_path: Path) -> None:
    path = tmp_path / "delivery.sqlite3"
    _, inbox = _stores(path)
    identity = _identity()
    inbox.register_and_claim(identity, owner="worker", lease_sec=30.0, now=1.0)

    durable_bytes = b"".join(
        candidate.read_bytes()
        for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm"))
        if candidate.exists()
    )
    for plaintext in (
        identity.event_key,
        identity.payload_sha256,
        identity.tenant_id,
        identity.meeting_id,
        identity.session_id,
        identity.analysis_run_id,
    ):
        assert plaintext.encode() not in durable_bytes


def test_key_rotation_finds_existing_encrypted_identity(tmp_path: Path) -> None:
    path = tmp_path / "delivery.sqlite3"
    first_store = SqliteOutboxStore(
        path,
        PayloadCipher({"v1": KEY}, "v1"),
        max_rows=10,
    )
    first_inbox = SqliteReadyEventInbox(first_store, max_rows=20, max_failures=3)
    claim = first_inbox.register_and_claim(_identity(), owner="worker", lease_sec=30.0, now=1.0)
    assert claim.lease_token is not None
    first_inbox.mark_dead(
        lookup_key=_identity().lookup_key,
        owner="worker",
        lease_token=claim.lease_token,
        error_code="terminal",
        reason=ReadyDeadReason.TERMINAL,
        now=2.0,
    )

    rotated_store = SqliteOutboxStore(
        path,
        PayloadCipher({"v1": KEY, "v2": b"N" * 32}, "v2"),
        max_rows=10,
    )
    rotated_inbox = SqliteReadyEventInbox(rotated_store, max_rows=20, max_failures=3)
    replay = rotated_inbox.register_and_claim(
        _identity(),
        owner="worker-2",
        lease_sec=30.0,
        now=3.0,
    )

    assert replay.disposition is ReadyClaimDisposition.DEAD
    assert rotated_inbox.summary(now=4.0).dead == 1

    with pytest.raises(OutboxKeyUnavailableError):
        SqliteOutboxStore(
            path,
            PayloadCipher({"v2": b"N" * 32}, "v2"),
            max_rows=10,
        )


def test_stale_lease_recovery_consumes_failure_budget(tmp_path: Path) -> None:
    _, inbox = _stores(tmp_path / "delivery.sqlite3")
    first = inbox.register_and_claim(_identity(), owner="worker-a", lease_sec=10.0, now=100.0)
    busy = inbox.register_and_claim(_identity(), owner="worker-b", lease_sec=10.0, now=105.0)
    recovered = inbox.register_and_claim(_identity(), owner="worker-b", lease_sec=10.0, now=111.0)

    assert first.disposition is ReadyClaimDisposition.CLAIMED
    assert busy.disposition is ReadyClaimDisposition.BUSY
    assert recovered.disposition is ReadyClaimDisposition.CLAIMED
    assert recovered.failure_count == 1
    assert recovered.lease_recovery_count == 1

    failed = inbox.mark_failure(
        lookup_key=_identity().lookup_key,
        owner="worker-b",
        lease_token=recovered.lease_token or "",
        error_code="transcript_http_503",
        next_attempt_at=120.0,
        max_failures=3,
        now=112.0,
    )
    assert failed.state is ReadyInboxState.RECEIVED
    assert failed.failure_count == 2


def test_repeated_stale_leases_stop_at_failure_budget(tmp_path: Path) -> None:
    path = tmp_path / "delivery.sqlite3"
    _, inbox = _stores(path)

    first = inbox.register_and_claim(_identity(), owner="worker-a", lease_sec=10.0, now=100.0)
    second = inbox.register_and_claim(_identity(), owner="worker-b", lease_sec=10.0, now=111.0)
    third = inbox.register_and_claim(_identity(), owner="worker-c", lease_sec=10.0, now=122.0)
    exhausted = inbox.register_and_claim(_identity(), owner="worker-d", lease_sec=10.0, now=133.0)

    assert first.disposition is ReadyClaimDisposition.CLAIMED
    assert second.disposition is ReadyClaimDisposition.CLAIMED
    assert second.failure_count == 1
    assert third.disposition is ReadyClaimDisposition.CLAIMED
    assert third.failure_count == 2
    assert exhausted.disposition is ReadyClaimDisposition.DEAD
    assert exhausted.failure_count == 3
    assert exhausted.lease_recovery_count == 3

    dead = inbox.list_dead(limit=10)
    assert len(dead) == 1
    assert dead[0].failure_count == 3
    assert dead[0].dead_reason is ReadyDeadReason.RETRY_EXHAUSTED
    assert dead[0].last_error_code == "lease_recovery_exhausted"


def test_not_due_exact_replay_uses_read_only_busy_fast_path(tmp_path: Path) -> None:
    path = tmp_path / "delivery.sqlite3"
    _, inbox = _stores(path)
    claim = inbox.register_and_claim(_identity(), owner="worker", lease_sec=10.0, now=100.0)
    assert claim.lease_token is not None
    inbox.mark_failure(
        lookup_key=_identity().lookup_key,
        owner="worker",
        lease_token=claim.lease_token,
        error_code="transcript_http_503",
        next_attempt_at=200.0,
        max_failures=3,
        now=101.0,
    )

    lock = sqlite3.connect(path, isolation_level=None)
    lock.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    try:
        busy = inbox.register_and_claim(
            _identity(),
            owner="worker-2",
            lease_sec=10.0,
            now=102.0,
        )
    finally:
        lock.rollback()
        lock.close()
    assert time.monotonic() - started < 1.0
    assert busy.disposition is ReadyClaimDisposition.BUSY
    assert busy.retry_after_sec == 98.0


def test_concurrent_duplicate_delivery_has_one_processing_owner(tmp_path: Path) -> None:
    _, inbox = _stores(tmp_path / "delivery.sqlite3")

    def claim(worker: int) -> ReadyClaimDisposition:
        return inbox.register_and_claim(
            _identity(),
            owner=f"worker-{worker}",
            lease_sec=30.0,
            now=100.0,
        ).disposition

    with ThreadPoolExecutor(max_workers=8) as executor:
        dispositions = list(executor.map(claim, range(8)))

    assert dispositions.count(ReadyClaimDisposition.CLAIMED) == 1
    assert dispositions.count(ReadyClaimDisposition.BUSY) == 7
    assert inbox.summary(now=101.0).processing == 1


def test_same_event_key_isolated_by_tenant(tmp_path: Path) -> None:
    _, inbox = _stores(tmp_path / "delivery.sqlite3")
    first = inbox.register_and_claim(_identity(), owner="worker-a", lease_sec=30.0, now=100.0)
    second_identity = _identity(
        tenant_id="44444444-4444-4444-8444-444444444444",
        run_id="run-2",
    )
    second = inbox.register_and_claim(second_identity, owner="worker-b", lease_sec=30.0, now=100.0)

    assert first.disposition is ReadyClaimDisposition.CLAIMED
    assert second.disposition is ReadyClaimDisposition.CLAIMED
    assert first.lease_token != second.lease_token
    assert inbox.summary(now=101.0).processing == 2


def test_recovered_lease_fences_stale_token_even_for_same_owner(tmp_path: Path) -> None:
    _, inbox = _stores(tmp_path / "delivery.sqlite3")
    first = inbox.register_and_claim(_identity(), owner="worker", lease_sec=10.0, now=100.0)
    recovered = inbox.register_and_claim(_identity(), owner="worker", lease_sec=10.0, now=111.0)
    assert first.lease_token is not None
    assert recovered.lease_token is not None
    assert first.lease_token != recovered.lease_token

    with pytest.raises(ReadyInboxLeaseLostError):
        inbox.commit_outboxed(
            lookup_key=_identity().lookup_key,
            owner="worker",
            lease_token=first.lease_token,
            analysis_run_id="run-1",
            meeting_id=_identity().meeting_id,
            payload={"summary": "stale-result"},
            now=112.0,
        )

    assert inbox.commit_outboxed(
        lookup_key=_identity().lookup_key,
        owner="worker",
        lease_token=recovered.lease_token,
        analysis_run_id="run-1",
        meeting_id=_identity().meeting_id,
        payload={"summary": "current-result"},
        now=112.0,
    )


def test_expired_lease_cannot_mutate_inbox(tmp_path: Path) -> None:
    _, inbox = _stores(tmp_path / "delivery.sqlite3")
    claim = inbox.register_and_claim(_identity(), owner="worker", lease_sec=10.0, now=100.0)
    assert claim.lease_token is not None

    with pytest.raises(ReadyInboxLeaseLostError):
        inbox.mark_failure(
            lookup_key=_identity().lookup_key,
            owner="worker",
            lease_token=claim.lease_token,
            error_code="late_failure",
            next_attempt_at=120.0,
            max_failures=3,
            now=111.0,
        )


def test_same_key_different_bytes_is_dead_even_after_outboxed(tmp_path: Path) -> None:
    _, inbox = _stores(tmp_path / "delivery.sqlite3")
    claim = inbox.register_and_claim(_identity(), owner="worker", lease_sec=30.0, now=10.0)
    assert claim.lease_token is not None
    assert inbox.commit_outboxed(
        lookup_key=_identity().lookup_key,
        owner="worker",
        lease_token=claim.lease_token,
        analysis_run_id="run-1",
        meeting_id=_identity().meeting_id,
        payload={"summary": "redacted-result"},
        now=11.0,
    )

    conflict = inbox.register_and_claim(
        _identity(payload_hash=HASH_B),
        owner="worker-2",
        lease_sec=30.0,
        now=12.0,
    )
    assert conflict.disposition is ReadyClaimDisposition.CONFLICT
    assert inbox.summary(now=13.0).dead == 1


def test_outbox_insert_and_outboxed_state_are_one_transaction(tmp_path: Path) -> None:
    outbox, inbox = _stores(tmp_path / "delivery.sqlite3")
    outbox.enqueue(
        analysis_run_id="run-1",
        meeting_id=_identity().meeting_id,
        payload={"summary": "first"},
        now=1.0,
    )
    claim = inbox.register_and_claim(_identity(), owner="worker", lease_sec=30.0, now=2.0)
    assert claim.lease_token is not None

    with pytest.raises(OutboxConflictError):
        inbox.commit_outboxed(
            lookup_key=_identity().lookup_key,
            owner="worker",
            lease_token=claim.lease_token,
            analysis_run_id="run-1",
            meeting_id=_identity().meeting_id,
            payload={"summary": "different"},
            now=3.0,
        )

    summary = inbox.summary(now=4.0)
    assert summary.processing == 1
    assert summary.outboxed == 0


def test_terminal_retention_waits_for_result_outbox_delivery(tmp_path: Path) -> None:
    outbox, inbox = _stores(tmp_path / "delivery.sqlite3")
    claim = inbox.register_and_claim(_identity(), owner="worker", lease_sec=30.0, now=1.0)
    assert claim.lease_token is not None
    inbox.commit_outboxed(
        lookup_key=_identity().lookup_key,
        owner="worker",
        lease_token=claim.lease_token,
        analysis_run_id="run-1",
        meeting_id=_identity().meeting_id,
        payload={"summary": "encrypted-result"},
        now=2.0,
    )

    assert inbox.prune_terminal(retention_sec=100.0, batch_size=10, now=200.0) == 0
    claimed = outbox.claim_next(owner="delivery", lease_sec=30.0, now=3.0)
    assert claimed is not None
    assert outbox.mark_delivered(analysis_run_id="run-1", owner="delivery")
    assert inbox.prune_terminal(retention_sec=100.0, batch_size=10, now=200.0) == 1
    assert inbox.summary(now=201.0).outboxed == 0


def test_poison_row_is_metadata_only_and_committed_before_dlq_marker(tmp_path: Path) -> None:
    path = tmp_path / "delivery.sqlite3"
    _, inbox = _stores(path)
    event_key, published = inbox.record_poison(
        source_message_id="123-0",
        payload_sha256=HASH_A,
        error_code="event_contract_invalid",
        now=1.0,
    )
    assert event_key == "invalid|123-0"
    assert not published
    inbox.mark_dlq_published(event_key, now=2.0)
    _, replay_published = inbox.record_poison(
        source_message_id="123-0",
        payload_sha256=HASH_A,
        error_code="event_contract_invalid",
        now=3.0,
    )
    assert replay_published
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            """
            SELECT event_key_digest, state, last_error_code
            FROM meeting_transcript_ready_inbox
            WHERE event_key_digest = ?
            """,
            (_lookup_digest(event_key),),
        ).fetchone()
    assert row == (_lookup_digest(event_key), "DEAD", "event_contract_invalid")


def test_retry_exhaustion_can_be_audit_rearmed_for_exact_producer_replay(tmp_path: Path) -> None:
    path = tmp_path / "delivery.sqlite3"
    _, inbox = _stores(path)
    claim = inbox.register_and_claim(_identity(), owner="worker", lease_sec=30.0, now=1.0)
    assert claim.lease_token is not None
    failed = inbox.mark_failure(
        lookup_key=_identity().lookup_key,
        owner="worker",
        lease_token=claim.lease_token,
        error_code="transcript_http_503",
        next_attempt_at=2.0,
        max_failures=1,
        now=2.0,
    )
    assert failed.state is ReadyInboxState.DEAD
    inbox.mark_dlq_published(_identity().lookup_key, now=3.0)
    dead = inbox.list_dead(limit=10)
    assert len(dead) == 1
    assert dead[0].dead_reason is ReadyDeadReason.RETRY_EXHAUSTED
    assert inbox.rearm_retry_exhausted(
        _identity().lookup_key,
        audit_reference="platform-ai#263/operator-review",
        now=4.0,
    )

    replay = inbox.register_and_claim(
        _identity(),
        owner="worker-2",
        lease_sec=30.0,
        now=5.0,
    )
    assert replay.disposition is ReadyClaimDisposition.CLAIMED
    assert replay.failure_count == 0
    assert replay.lease_recovery_count == 0
    with sqlite3.connect(path) as connection:
        audit = connection.execute(
            """
                SELECT redrive_count, last_redriven_at, last_redrive_reference
                FROM meeting_transcript_ready_inbox WHERE event_key_digest = ?
                """,
            (_lookup_digest(_identity().lookup_key),),
        ).fetchone()
    assert audit == (1, 4.0, "platform-ai#263/operator-review")


def test_poison_and_payload_conflict_dead_rows_cannot_be_rearmed(tmp_path: Path) -> None:
    _, inbox = _stores(tmp_path / "delivery.sqlite3")
    poison_key, _ = inbox.record_poison(
        source_message_id="9-0",
        payload_sha256=HASH_A,
        error_code="event_contract_invalid",
        now=1.0,
    )
    inbox.mark_dlq_published(poison_key, now=2.0)
    assert not inbox.rearm_retry_exhausted(
        poison_key,
        audit_reference="platform-ai#263",
        now=3.0,
    )

    inbox.register_and_claim(_identity(), owner="worker", lease_sec=30.0, now=4.0)
    conflict = inbox.register_and_claim(
        _identity(payload_hash=HASH_B),
        owner="worker-2",
        lease_sec=30.0,
        now=5.0,
    )
    assert conflict.disposition is ReadyClaimDisposition.CONFLICT
    inbox.mark_dlq_published(_identity().lookup_key, now=6.0)
    assert not inbox.rearm_retry_exhausted(
        _identity().lookup_key,
        audit_reference="platform-ai#263",
        now=7.0,
    )
