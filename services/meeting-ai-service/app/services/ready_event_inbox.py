"""Encrypted metadata-only inbox for ``meeting.transcript.ready`` events.

Raw event JSON and transcript text are intentionally excluded. The original
wire bytes are represented only by a SHA-256 digest so a reused event key with
different bytes fails closed. Identity metadata is held in an AES-256-GCM
envelope; only a keyed event-key lookup digest and operational state remain
queryable in SQLite.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from enum import Enum

from app.services.durable_outbox import OutboxError, OutboxIntegrityError, SqliteOutboxStore

_IDENTITY_PURPOSE = "ready-event-inbox"


class ReadyInboxError(OutboxError):
    """Base class for ready-event inbox failures."""


class ReadyInboxFullError(ReadyInboxError):
    """The bounded durable inbox cannot accept another event identity."""


class ReadyInboxLeaseLostError(ReadyInboxError):
    """The caller no longer owns the processing lease."""


class ReadyInboxIntegrityError(ReadyInboxError):
    """An encrypted inbox identity cannot be authenticated or validated."""


class ReadyInboxState(str, Enum):
    RECEIVED = "RECEIVED"
    PROCESSING = "PROCESSING"
    OUTBOXED = "OUTBOXED"
    DEAD = "DEAD"


class ReadyDeadReason(str, Enum):
    RETRY_EXHAUSTED = "RETRY_EXHAUSTED"
    TERMINAL = "TERMINAL"
    CONFLICT = "CONFLICT"
    POISON = "POISON"


class ReadyClaimDisposition(str, Enum):
    CLAIMED = "CLAIMED"
    BUSY = "BUSY"
    OUTBOXED = "OUTBOXED"
    DEAD = "DEAD"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True)
class ReadyEventIdentity:
    event_key: str
    payload_sha256: str
    tenant_id: str
    meeting_id: str
    session_id: str
    finalization_version: int
    analysis_run_id: str


@dataclass(frozen=True)
class ReadyClaim:
    disposition: ReadyClaimDisposition
    failure_count: int
    lease_recovery_count: int
    dlq_published: bool
    retry_after_sec: float | None = None


@dataclass(frozen=True)
class ReadyFailureResult:
    state: ReadyInboxState
    failure_count: int


@dataclass(frozen=True)
class ReadyInboxSummary:
    received: int
    processing: int
    outboxed: int
    dead: int
    oldest_unfinished_age_sec: float | None


@dataclass(frozen=True)
class ReadyDeadLetterMetadata:
    """Transcript-free operator view of a ready-event dead-letter row."""

    event_key: str
    failure_count: int
    dead_reason: ReadyDeadReason | None
    last_error_code: str | None
    updated_at: float
    redrive_count: int
    last_redriven_at: float | None
    last_redrive_reference: str | None


class SqliteReadyEventInbox:
    """Lease-based inbox sharing one WAL database with the result outbox."""

    def __init__(
        self,
        outbox: SqliteOutboxStore,
        *,
        max_rows: int,
        max_failures: int,
    ) -> None:
        if max_rows < 1:
            raise ValueError("max_rows must be positive")
        if max_failures < 1:
            raise ValueError("max_failures must be positive")
        self._outbox = outbox
        self._max_rows = max_rows
        self._max_failures = max_failures

    def _connect(self) -> sqlite3.Connection:
        return self._outbox._connect()

    def _lookup_digests(self, event_key: str) -> tuple[str, ...]:
        return self._outbox._cipher.lookup_digests(
            purpose=_IDENTITY_PURPOSE,
            value=event_key,
        )

    @staticmethod
    def _lookup_clause(digests: tuple[str, ...]) -> str:
        return ", ".join("?" for _ in digests)

    def _select_by_event_key(
        self,
        connection: sqlite3.Connection,
        event_key: str,
        *,
        columns: str = "*",
    ) -> sqlite3.Row | None:
        digests = self._lookup_digests(event_key)
        row: sqlite3.Row | None = connection.execute(
            f"""
            SELECT {columns}
            FROM meeting_transcript_ready_inbox
            WHERE event_key_digest IN ({self._lookup_clause(digests)})
            """,  # noqa: S608 - placeholders are generated, not caller-controlled SQL
            digests,
        ).fetchone()
        return row

    def _encrypt_identity(
        self,
        identity: ReadyEventIdentity,
    ) -> tuple[str, str, bytes, bytes]:
        return self._encrypt_identity_values(
            event_key=identity.event_key,
            payload_sha256=identity.payload_sha256,
            tenant_id=identity.tenant_id,
            meeting_id=identity.meeting_id,
            session_id=identity.session_id,
            finalization_version=identity.finalization_version,
            analysis_run_id=identity.analysis_run_id,
        )

    def _encrypt_identity_values(
        self,
        *,
        event_key: str,
        payload_sha256: str,
        tenant_id: str | None,
        meeting_id: str | None,
        session_id: str | None,
        finalization_version: int | None,
        analysis_run_id: str | None,
    ) -> tuple[str, str, bytes, bytes]:
        return self._outbox._cipher.encrypt_metadata(
            purpose=_IDENTITY_PURPOSE,
            lookup_value=event_key,
            payload={
                "eventKey": event_key,
                "payloadSha256": payload_sha256,
                "tenantId": tenant_id,
                "meetingId": meeting_id,
                "sessionId": session_id,
                "finalizationVersion": finalization_version,
                "analysisRunId": analysis_run_id,
            },
        )

    def _decrypt_identity(self, row: sqlite3.Row) -> ReadyEventIdentity:
        try:
            envelope = self._outbox._cipher.decrypt_metadata(
                purpose=_IDENTITY_PURPOSE,
                lookup_digest=str(row["event_key_digest"]),
                key_id=str(row["identity_key_id"]),
                nonce=bytes(row["identity_nonce"]),
                ciphertext=bytes(row["identity_ciphertext"]),
            )
            event_key = envelope["eventKey"]
            payload_sha256 = envelope["payloadSha256"]
            tenant_id = envelope["tenantId"]
            meeting_id = envelope["meetingId"]
            session_id = envelope["sessionId"]
            finalization_version = envelope["finalizationVersion"]
            analysis_run_id = envelope["analysisRunId"]
        except (KeyError, TypeError, ValueError, OutboxIntegrityError) as exc:
            raise ReadyInboxIntegrityError("ready-event identity envelope is invalid") from exc
        if (
            not isinstance(event_key, str)
            or not isinstance(payload_sha256, str)
            or not isinstance(tenant_id, str)
            or not isinstance(meeting_id, str)
            or not isinstance(session_id, str)
            or not isinstance(analysis_run_id, str)
            or not isinstance(finalization_version, int)
        ):
            raise ReadyInboxIntegrityError("ready-event identity envelope has invalid types")
        expected_digests = self._lookup_digests(event_key)
        if str(row["event_key_digest"]) not in expected_digests:
            raise ReadyInboxIntegrityError("ready-event identity lookup digest is invalid")
        return ReadyEventIdentity(
            event_key=event_key,
            payload_sha256=payload_sha256,
            tenant_id=tenant_id,
            meeting_id=meeting_id,
            session_id=session_id,
            finalization_version=finalization_version,
            analysis_run_id=analysis_run_id,
        )

    def _decrypt_event_metadata(self, row: sqlite3.Row) -> tuple[str, str, str | None]:
        """Decrypt event key, payload digest, and optional analysis run id."""
        try:
            envelope = self._outbox._cipher.decrypt_metadata(
                purpose=_IDENTITY_PURPOSE,
                lookup_digest=str(row["event_key_digest"]),
                key_id=str(row["identity_key_id"]),
                nonce=bytes(row["identity_nonce"]),
                ciphertext=bytes(row["identity_ciphertext"]),
            )
            event_key = envelope["eventKey"]
            payload_sha256 = envelope["payloadSha256"]
            analysis_run_id = envelope.get("analysisRunId")
        except (KeyError, TypeError, ValueError, OutboxIntegrityError) as exc:
            raise ReadyInboxIntegrityError("ready-event metadata envelope is invalid") from exc
        if not isinstance(event_key, str) or not isinstance(payload_sha256, str):
            raise ReadyInboxIntegrityError("ready-event metadata envelope has invalid types")
        if analysis_run_id is not None and not isinstance(analysis_run_id, str):
            raise ReadyInboxIntegrityError("ready-event analysis run id has invalid type")
        if str(row["event_key_digest"]) not in self._lookup_digests(event_key):
            raise ReadyInboxIntegrityError("ready-event metadata lookup digest is invalid")
        return event_key, payload_sha256, analysis_run_id

    def register_and_claim(
        self,
        identity: ReadyEventIdentity,
        *,
        owner: str,
        lease_sec: float,
        now: float | None = None,
    ) -> ReadyClaim:
        timestamp = time.time() if now is None else now
        busy = self._read_only_busy_claim(identity, now=timestamp)
        if busy is not None:
            return busy
        lease_until = timestamp + lease_sec
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._select_by_event_key(connection, identity.event_key)
                if row is None:
                    row_count = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM meeting_transcript_ready_inbox"
                        ).fetchone()[0]
                    )
                    if row_count >= self._max_rows:
                        raise ReadyInboxFullError("durable ready-event inbox is full")
                    key_id, event_key_digest, nonce, ciphertext = self._encrypt_identity(identity)
                    connection.execute(
                        """
                        INSERT INTO meeting_transcript_ready_inbox (
                            event_key_digest, identity_key_id, identity_nonce,
                            identity_ciphertext, state,
                            failure_count, lease_recovery_count, next_attempt_at,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, 'RECEIVED', 0, 0, ?, ?, ?)
                        """,
                        (
                            event_key_digest,
                            key_id,
                            nonce,
                            ciphertext,
                            timestamp,
                            timestamp,
                            timestamp,
                        ),
                    )
                    row = self._select_by_event_key(connection, identity.event_key)
                    assert row is not None

                failure_count = int(row["failure_count"])
                recovery_count = int(row["lease_recovery_count"])
                dlq_published = row["dlq_published_at"] is not None
                stored_identity = self._decrypt_identity(row)
                if stored_identity.event_key != identity.event_key:
                    raise ReadyInboxIntegrityError("ready-event lookup returned another identity")
                if stored_identity.payload_sha256 != identity.payload_sha256:
                    conflict_already_published = (
                        str(row["state"]) == ReadyInboxState.DEAD.value
                        and row["dead_reason"] == ReadyDeadReason.CONFLICT.value
                        and dlq_published
                    )
                    connection.execute(
                        """
                        UPDATE meeting_transcript_ready_inbox
                        SET state = 'DEAD', lease_owner = NULL, lease_until = NULL,
                            updated_at = ?, last_error_code = ?, dead_reason = 'CONFLICT',
                            dlq_published_at = ?
                        WHERE event_key_digest = ?
                        """,
                        (
                            timestamp,
                            "event_payload_hash_conflict",
                            row["dlq_published_at"] if conflict_already_published else None,
                            row["event_key_digest"],
                        ),
                    )
                    connection.commit()
                    return ReadyClaim(
                        ReadyClaimDisposition.CONFLICT,
                        failure_count,
                        recovery_count,
                        conflict_already_published,
                    )

                state = ReadyInboxState(str(row["state"]))
                if state is ReadyInboxState.OUTBOXED:
                    connection.commit()
                    return ReadyClaim(
                        ReadyClaimDisposition.OUTBOXED,
                        failure_count,
                        recovery_count,
                        dlq_published,
                    )
                if state is ReadyInboxState.DEAD:
                    connection.commit()
                    return ReadyClaim(
                        ReadyClaimDisposition.DEAD,
                        failure_count,
                        recovery_count,
                        dlq_published,
                    )

                if state is ReadyInboxState.PROCESSING:
                    lease_expired = float(row["lease_until"]) <= timestamp
                    if not lease_expired:
                        connection.commit()
                        return ReadyClaim(
                            ReadyClaimDisposition.BUSY,
                            failure_count,
                            recovery_count,
                            dlq_published,
                            max(0.0, float(row["lease_until"]) - timestamp),
                        )
                    recovery_count += 1
                    failure_count += 1
                    if failure_count >= self._max_failures:
                        connection.execute(
                            """
                            UPDATE meeting_transcript_ready_inbox
                            SET state = 'DEAD', failure_count = ?,
                                lease_recovery_count = ?, lease_owner = NULL,
                                lease_until = NULL, updated_at = ?,
                                last_error_code = 'lease_recovery_exhausted',
                                dead_reason = 'RETRY_EXHAUSTED'
                            WHERE event_key_digest = ? AND state = 'PROCESSING'
                            """,
                            (
                                failure_count,
                                recovery_count,
                                timestamp,
                                row["event_key_digest"],
                            ),
                        )
                        connection.commit()
                        return ReadyClaim(
                            ReadyClaimDisposition.DEAD,
                            failure_count,
                            recovery_count,
                            dlq_published,
                        )
                elif float(row["next_attempt_at"]) > timestamp:
                    connection.commit()
                    return ReadyClaim(
                        ReadyClaimDisposition.BUSY,
                        failure_count,
                        recovery_count,
                        dlq_published,
                        max(0.0, float(row["next_attempt_at"]) - timestamp),
                    )

                connection.execute(
                    """
                    UPDATE meeting_transcript_ready_inbox
                    SET state = 'PROCESSING', lease_owner = ?, lease_until = ?,
                        failure_count = ?, lease_recovery_count = ?, updated_at = ?
                    WHERE event_key_digest = ?
                    """,
                    (
                        owner,
                        lease_until,
                        failure_count,
                        recovery_count,
                        timestamp,
                        row["event_key_digest"],
                    ),
                )
                connection.commit()
                return ReadyClaim(
                    ReadyClaimDisposition.CLAIMED,
                    failure_count,
                    recovery_count,
                    dlq_published,
                )
            except Exception:
                connection.rollback()
                raise

    def _read_only_busy_claim(
        self,
        identity: ReadyEventIdentity,
        *,
        now: float,
    ) -> ReadyClaim | None:
        """Avoid a SQLite writer lock when an exact replay is not claimable yet."""
        with closing(self._connect()) as connection:
            row = self._select_by_event_key(connection, identity.event_key)
        if row is None:
            return None
        stored_identity = self._decrypt_identity(row)
        if (
            stored_identity.event_key != identity.event_key
            or stored_identity.payload_sha256 != identity.payload_sha256
        ):
            return None
        state = ReadyInboxState(str(row["state"]))
        available_at: float | None = None
        if state is ReadyInboxState.RECEIVED and float(row["next_attempt_at"]) > now:
            available_at = float(row["next_attempt_at"])
        elif (
            state is ReadyInboxState.PROCESSING
            and row["lease_until"] is not None
            and float(row["lease_until"]) > now
        ):
            available_at = float(row["lease_until"])
        if available_at is None:
            return None
        return ReadyClaim(
            ReadyClaimDisposition.BUSY,
            int(row["failure_count"]),
            int(row["lease_recovery_count"]),
            row["dlq_published_at"] is not None,
            max(0.0, available_at - now),
        )

    def record_poison(
        self,
        *,
        source_message_id: str,
        payload_sha256: str,
        error_code: str,
        now: float | None = None,
    ) -> tuple[str, bool]:
        """Durably record an unparseable message before its Redis acknowledgement."""
        timestamp = time.time() if now is None else now
        synthetic_key = f"invalid|{source_message_id}"
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._select_by_event_key(connection, synthetic_key)
                if row is None:
                    row_count = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM meeting_transcript_ready_inbox"
                        ).fetchone()[0]
                    )
                    if row_count >= self._max_rows:
                        raise ReadyInboxFullError("durable ready-event inbox is full")
                    key_id, event_key_digest, nonce, ciphertext = self._encrypt_identity_values(
                        event_key=synthetic_key,
                        payload_sha256=payload_sha256,
                        tenant_id=None,
                        meeting_id=None,
                        session_id=None,
                        finalization_version=None,
                        analysis_run_id=None,
                    )
                    connection.execute(
                        """
                        INSERT INTO meeting_transcript_ready_inbox (
                            event_key_digest, identity_key_id, identity_nonce,
                            identity_ciphertext, state, failure_count,
                            lease_recovery_count, next_attempt_at, created_at,
                            updated_at, last_error_code, dead_reason
                        ) VALUES (?, ?, ?, ?, 'DEAD', 0, 0, ?, ?, ?, ?, 'POISON')
                        """,
                        (
                            event_key_digest,
                            key_id,
                            nonce,
                            ciphertext,
                            timestamp,
                            timestamp,
                            timestamp,
                            error_code[:128],
                        ),
                    )
                    published = False
                else:
                    stored_key, stored_hash, _ = self._decrypt_event_metadata(row)
                    if stored_key != synthetic_key or stored_hash != payload_sha256:
                        raise ReadyInboxIntegrityError(
                            "poison event identity changed for one Redis message id"
                        )
                    published = row["dlq_published_at"] is not None
                connection.commit()
                return synthetic_key, published
            except Exception:
                connection.rollback()
                raise

    def mark_failure(
        self,
        *,
        event_key: str,
        owner: str,
        error_code: str,
        next_attempt_at: float,
        max_failures: int,
        now: float | None = None,
    ) -> ReadyFailureResult:
        timestamp = time.time() if now is None else now
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._select_by_event_key(connection, event_key)
                if (
                    row is None
                    or str(row["state"]) != ReadyInboxState.PROCESSING.value
                    or str(row["lease_owner"]) != owner
                ):
                    raise ReadyInboxLeaseLostError("ready-event processing lease was lost")
                stored_key, _, _ = self._decrypt_event_metadata(row)
                if stored_key != event_key:
                    raise ReadyInboxIntegrityError("ready-event lookup returned another identity")
                failure_count = int(row["failure_count"]) + 1
                state = (
                    ReadyInboxState.DEAD
                    if failure_count >= max_failures
                    else ReadyInboxState.RECEIVED
                )
                connection.execute(
                    """
                    UPDATE meeting_transcript_ready_inbox
                    SET state = ?, failure_count = ?, next_attempt_at = ?,
                        lease_owner = NULL, lease_until = NULL, updated_at = ?,
                        last_error_code = ?, dead_reason = ?
                    WHERE event_key_digest = ? AND state = 'PROCESSING' AND lease_owner = ?
                    """,
                    (
                        state.value,
                        failure_count,
                        next_attempt_at,
                        timestamp,
                        error_code[:128],
                        (
                            ReadyDeadReason.RETRY_EXHAUSTED.value
                            if state is ReadyInboxState.DEAD
                            else None
                        ),
                        row["event_key_digest"],
                        owner,
                    ),
                )
                connection.commit()
                return ReadyFailureResult(state, failure_count)
            except Exception:
                connection.rollback()
                raise

    def defer_without_failure(
        self,
        *,
        event_key: str,
        owner: str,
        error_code: str,
        next_attempt_at: float,
        now: float | None = None,
    ) -> None:
        """Release a lease for local backpressure without consuming attempt budget."""
        timestamp = time.time() if now is None else now
        with closing(self._connect()) as connection:
            row = self._select_by_event_key(connection, event_key)
            digest = None if row is None else str(row["event_key_digest"])
            cursor = connection.execute(
                """
                UPDATE meeting_transcript_ready_inbox
                SET state = 'RECEIVED', next_attempt_at = ?, lease_owner = NULL,
                    lease_until = NULL, updated_at = ?, last_error_code = ?,
                    dead_reason = NULL
                WHERE event_key_digest = ? AND state = 'PROCESSING' AND lease_owner = ?
                """,
                (next_attempt_at, timestamp, error_code[:128], digest, owner),
            )
        if cursor.rowcount != 1:
            raise ReadyInboxLeaseLostError("ready-event processing lease was lost")

    def mark_dead(
        self,
        *,
        event_key: str,
        owner: str,
        error_code: str,
        reason: ReadyDeadReason,
        now: float | None = None,
    ) -> None:
        timestamp = time.time() if now is None else now
        with closing(self._connect()) as connection:
            row = self._select_by_event_key(connection, event_key)
            digest = None if row is None else str(row["event_key_digest"])
            cursor = connection.execute(
                """
                UPDATE meeting_transcript_ready_inbox
                SET state = 'DEAD', lease_owner = NULL, lease_until = NULL,
                    updated_at = ?, last_error_code = ?, dead_reason = ?
                WHERE event_key_digest = ? AND state = 'PROCESSING' AND lease_owner = ?
                """,
                (timestamp, error_code[:128], reason.value, digest, owner),
            )
        if cursor.rowcount != 1:
            raise ReadyInboxLeaseLostError("ready-event processing lease was lost")

    def mark_dlq_published(self, event_key: str, *, now: float | None = None) -> None:
        timestamp = time.time() if now is None else now
        with closing(self._connect()) as connection:
            row = self._select_by_event_key(connection, event_key)
            digest = None if row is None else str(row["event_key_digest"])
            cursor = connection.execute(
                """
                UPDATE meeting_transcript_ready_inbox
                SET dlq_published_at = ?, updated_at = ?
                WHERE event_key_digest = ? AND state = 'DEAD'
                """,
                (timestamp, timestamp, digest),
            )
        if cursor.rowcount != 1:
            raise ReadyInboxError("cannot mark DLQ publication for a non-dead inbox row")

    def list_dead(self, *, limit: int = 100) -> list[ReadyDeadLetterMetadata]:
        if not 1 <= limit <= 1000:
            raise ValueError("dead-letter list limit must be between 1 and 1000")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT event_key_digest, identity_key_id, identity_nonce,
                       identity_ciphertext, failure_count, dead_reason, last_error_code,
                       updated_at, redrive_count, last_redriven_at,
                       last_redrive_reference
                FROM meeting_transcript_ready_inbox
                WHERE state = 'DEAD'
                ORDER BY updated_at DESC, event_key_digest
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            ReadyDeadLetterMetadata(
                event_key=self._decrypt_event_metadata(row)[0],
                failure_count=int(row["failure_count"]),
                dead_reason=(
                    None if row["dead_reason"] is None else ReadyDeadReason(str(row["dead_reason"]))
                ),
                last_error_code=(
                    None if row["last_error_code"] is None else str(row["last_error_code"])
                ),
                updated_at=float(row["updated_at"]),
                redrive_count=int(row["redrive_count"]),
                last_redriven_at=(
                    None if row["last_redriven_at"] is None else float(row["last_redriven_at"])
                ),
                last_redrive_reference=(
                    None
                    if row["last_redrive_reference"] is None
                    else str(row["last_redrive_reference"])
                ),
            )
            for row in rows
        ]

    def rearm_retry_exhausted(
        self,
        event_key: str,
        *,
        audit_reference: str,
        now: float | None = None,
    ) -> bool:
        """Rearm an operator-reviewed retry exhaustion before exact producer replay.

        Poison, contract-terminal, and payload-conflict rows can never be rearmed.
        The event body is intentionally absent, so the producer must replay the
        exact original event after this transition.
        """
        reference = audit_reference.strip()
        if not reference or len(reference) > 128:
            raise ValueError("audit_reference must contain 1 to 128 characters")
        timestamp = time.time() if now is None else now
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._select_by_event_key(connection, event_key)
                if (
                    row is None
                    or str(row["state"]) != ReadyInboxState.DEAD.value
                    or row["dead_reason"] != ReadyDeadReason.RETRY_EXHAUSTED.value
                    or row["dlq_published_at"] is None
                ):
                    connection.commit()
                    return False
                stored_key, _, analysis_run_id = self._decrypt_event_metadata(row)
                if stored_key != event_key or analysis_run_id is None:
                    raise ReadyInboxIntegrityError("retry-exhausted identity is invalid")
                result_exists = connection.execute(
                    "SELECT 1 FROM analysis_delivery_outbox WHERE analysis_run_id = ?",
                    (analysis_run_id,),
                ).fetchone()
                if result_exists is not None:
                    connection.commit()
                    return False
                cursor = connection.execute(
                    """
                    UPDATE meeting_transcript_ready_inbox
                    SET state = 'RECEIVED', failure_count = 0,
                        lease_recovery_count = 0,
                        next_attempt_at = ?, updated_at = ?, last_error_code = NULL,
                        dlq_published_at = NULL, dead_reason = NULL,
                        redrive_count = redrive_count + 1, last_redriven_at = ?,
                        last_redrive_reference = ?
                    WHERE event_key_digest = ?
                      AND state = 'DEAD'
                      AND dead_reason = 'RETRY_EXHAUSTED'
                      AND dlq_published_at IS NOT NULL
                    """,
                    (
                        timestamp,
                        timestamp,
                        timestamp,
                        reference,
                        row["event_key_digest"],
                    ),
                )
                connection.commit()
                return cursor.rowcount == 1
            except Exception:
                connection.rollback()
                raise

    def commit_outboxed(
        self,
        *,
        event_key: str,
        owner: str,
        analysis_run_id: str,
        meeting_id: str,
        payload: dict[str, object],
        now: float | None = None,
    ) -> bool:
        timestamp = time.time() if now is None else now
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._select_by_event_key(connection, event_key)
                if (
                    row is None
                    or str(row["state"]) != ReadyInboxState.PROCESSING.value
                    or str(row["lease_owner"]) != owner
                ):
                    raise ReadyInboxLeaseLostError("ready-event processing lease was lost")
                stored_key, _, stored_analysis_run_id = self._decrypt_event_metadata(row)
                if stored_key != event_key or stored_analysis_run_id != analysis_run_id:
                    raise ReadyInboxLeaseLostError("ready-event processing lease was lost")
                inserted = self._outbox.enqueue_in_transaction(
                    connection,
                    analysis_run_id=analysis_run_id,
                    meeting_id=meeting_id,
                    payload=payload,
                    now=timestamp,
                )
                cursor = connection.execute(
                    """
                    UPDATE meeting_transcript_ready_inbox
                    SET state = 'OUTBOXED', lease_owner = NULL, lease_until = NULL,
                        updated_at = ?, last_error_code = NULL
                    WHERE event_key_digest = ? AND state = 'PROCESSING' AND lease_owner = ?
                    """,
                    (timestamp, row["event_key_digest"], owner),
                )
                if cursor.rowcount != 1:
                    raise ReadyInboxLeaseLostError("ready-event processing lease was lost")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        self._outbox._restrict_database_files()
        return inserted

    def prune_terminal(
        self,
        *,
        retention_sec: float,
        batch_size: int,
        now: float | None = None,
    ) -> int:
        """Delete old terminal identities only after result-outbox ownership ends.

        The retention window must cover the producer replay horizon. An
        ``OUTBOXED``/``DEAD`` row whose analysis result still exists locally is
        never removed, so replay cannot race a pending or operator-held result.
        """
        if retention_sec <= 0:
            raise ValueError("retention_sec must be positive")
        if not 1 <= batch_size <= 10_000:
            raise ValueError("batch_size must be between 1 and 10000")
        timestamp = time.time() if now is None else now
        cutoff = timestamp - retention_sec
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                rows = connection.execute(
                    """
                    SELECT event_key_digest, identity_key_id, identity_nonce,
                           identity_ciphertext, state
                    FROM meeting_transcript_ready_inbox
                    WHERE state IN ('OUTBOXED', 'DEAD')
                      AND updated_at <= ?
                      AND (state = 'OUTBOXED' OR dlq_published_at IS NOT NULL)
                    ORDER BY updated_at, event_key_digest
                    """,
                    (cutoff,),
                )
                digests: list[str] = []
                for row in rows:
                    _, _, analysis_run_id = self._decrypt_event_metadata(row)
                    if analysis_run_id is not None:
                        result_exists = connection.execute(
                            "SELECT 1 FROM analysis_delivery_outbox WHERE analysis_run_id = ?",
                            (analysis_run_id,),
                        ).fetchone()
                        if result_exists is not None:
                            continue
                    digests.append(str(row["event_key_digest"]))
                    if len(digests) >= batch_size:
                        break
                if digests:
                    connection.executemany(
                        "DELETE FROM meeting_transcript_ready_inbox WHERE event_key_digest = ?",
                        ((digest,) for digest in digests),
                    )
                connection.commit()
                return len(digests)
            except Exception:
                connection.rollback()
                raise

    def summary(self, *, now: float | None = None) -> ReadyInboxSummary:
        timestamp = time.time() if now is None else now
        with closing(self._connect()) as connection:
            counts = {
                str(row["state"]): int(row["count"])
                for row in connection.execute(
                    """
                    SELECT state, COUNT(*) AS count
                    FROM meeting_transcript_ready_inbox
                    GROUP BY state
                    """
                ).fetchall()
            }
            oldest = connection.execute(
                """
                SELECT MIN(created_at)
                FROM meeting_transcript_ready_inbox
                WHERE state IN ('RECEIVED', 'PROCESSING')
                """
            ).fetchone()[0]
        return ReadyInboxSummary(
            received=counts.get(ReadyInboxState.RECEIVED.value, 0),
            processing=counts.get(ReadyInboxState.PROCESSING.value, 0),
            outboxed=counts.get(ReadyInboxState.OUTBOXED.value, 0),
            dead=counts.get(ReadyInboxState.DEAD.value, 0),
            oldest_unfinished_age_sec=(
                None if oldest is None else max(0.0, timestamp - float(oldest))
            ),
        )

    def has_capacity(self) -> bool:
        with closing(self._connect()) as connection:
            row_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM meeting_transcript_ready_inbox"
                ).fetchone()[0]
            )
        return row_count < self._max_rows
