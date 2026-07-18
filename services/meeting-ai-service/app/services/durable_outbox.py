"""Encrypted SQLite outbox for analysis-result delivery.

The outbox is deliberately embedded: the current meeting-ai runtime is a
single Windows GPU host, and delivery must survive process/network failures
without making Redis availability part of the user-visible analyze path.
SQLite WAL + ``BEGIN IMMEDIATE`` provides process-safe leases on one local
filesystem. The store is behind a small API so a shared PostgreSQL/Kafka/Redis
adapter can replace it before horizontal multi-host execution.

KVKK boundary: payloads contain redacted meeting intelligence and are still
sensitive. They are AES-256-GCM encrypted before SQLite sees them. Raw
transcripts are never accepted by this module.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from enum import Enum
from importlib.resources import files
from pathlib import Path
from typing import Any, ClassVar

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class OutboxState(str, Enum):
    PENDING = "PENDING"
    IN_FLIGHT = "IN_FLIGHT"
    DEAD = "DEAD"


class OutboxError(RuntimeError):
    """Base class for durable outbox failures."""


class OutboxFullError(OutboxError):
    """The bounded queue cannot accept more sensitive payloads."""


class OutboxConflictError(OutboxError):
    """An analysis-run id was reused with a different semantic payload."""


class OutboxKeyUnavailableError(OutboxError):
    """An encrypted row references a key that is not in the runtime keyring."""


class OutboxIntegrityError(OutboxError):
    """AES-GCM authentication failed for a persisted payload."""


@dataclass(frozen=True)
class ClaimedMessage:
    analysis_run_id: str
    meeting_id: str
    payload: dict[str, object]
    attempt_count: int
    created_at: float


@dataclass(frozen=True)
class OutboxSummary:
    pending: int
    in_flight: int
    dead: int
    oldest_pending_age_sec: float | None


@dataclass(frozen=True)
class DeadLetterMetadata:
    """Payload-free operator view of one dead-letter row."""

    analysis_run_id: str
    attempt_count: int
    created_at: float
    updated_at: float
    last_error_code: str | None


class PayloadCipher:
    """Versioned AES-GCM keyring with per-message authenticated context."""

    def __init__(self, keys: dict[str, bytes], active_key_id: str) -> None:
        invalid_key_ids = sorted(key_id for key_id, key in keys.items() if len(key) != 32)
        if invalid_key_ids:
            raise OutboxKeyUnavailableError(
                "outbox AES-256 keys must be exactly 32 bytes: " + ", ".join(invalid_key_ids)
            )
        if active_key_id not in keys:
            raise OutboxKeyUnavailableError("active key id is missing from the keyring")
        self._keys = dict(keys)
        self.active_key_id = active_key_id

    @property
    def key_ids(self) -> frozenset[str]:
        return frozenset(self._keys)

    @staticmethod
    def _aad(analysis_run_id: str, meeting_id: str) -> bytes:
        return f"meeting-ai-outbox:v1:{analysis_run_id}:{meeting_id}".encode()

    def encrypt(
        self,
        analysis_run_id: str,
        meeting_id: str,
        payload: dict[str, object],
    ) -> tuple[str, bytes, bytes, str]:
        plaintext = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(self._keys[self.active_key_id]).encrypt(
            nonce,
            plaintext,
            self._aad(analysis_run_id, meeting_id),
        )
        return (
            self.active_key_id,
            nonce,
            ciphertext,
            hashlib.sha256(plaintext).hexdigest(),
        )

    def decrypt(
        self,
        *,
        key_id: str,
        nonce: bytes,
        ciphertext: bytes,
        analysis_run_id: str,
        meeting_id: str,
    ) -> dict[str, object]:
        key = self._keys.get(key_id)
        if key is None:
            raise OutboxKeyUnavailableError(f"outbox key id {key_id!r} is unavailable")
        try:
            plaintext = AESGCM(key).decrypt(
                nonce,
                ciphertext,
                self._aad(analysis_run_id, meeting_id),
            )
        except InvalidTag as exc:
            raise OutboxIntegrityError("outbox payload authentication failed") from exc
        parsed: Any = json.loads(plaintext)
        if not isinstance(parsed, dict):
            raise OutboxIntegrityError("outbox payload is not a JSON object")
        return parsed

    def lookup_digests(self, *, purpose: str, value: str) -> tuple[str, ...]:
        """Return keyed deterministic digests for rotation-safe encrypted lookups."""
        key_ids = (self.active_key_id, *sorted(self._keys.keys() - {self.active_key_id}))
        message = f"meeting-ai-lookup:v1:{purpose}:{value}".encode()
        return tuple(
            hmac.new(self._keys[key_id], message, hashlib.sha256).hexdigest() for key_id in key_ids
        )

    def encrypt_metadata(
        self,
        *,
        purpose: str,
        lookup_value: str,
        payload: dict[str, object],
    ) -> tuple[str, str, bytes, bytes]:
        """Encrypt a metadata envelope and return its keyed lookup digest."""
        lookup_digest = self.lookup_digests(purpose=purpose, value=lookup_value)[0]
        plaintext = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(self._keys[self.active_key_id]).encrypt(
            nonce,
            plaintext,
            self._metadata_aad(purpose, lookup_digest),
        )
        return self.active_key_id, lookup_digest, nonce, ciphertext

    def decrypt_metadata(
        self,
        *,
        purpose: str,
        lookup_digest: str,
        key_id: str,
        nonce: bytes,
        ciphertext: bytes,
    ) -> dict[str, object]:
        """Authenticate and decrypt one metadata envelope."""
        key = self._keys.get(key_id)
        if key is None:
            raise OutboxKeyUnavailableError(f"metadata key id {key_id!r} is unavailable")
        try:
            plaintext = AESGCM(key).decrypt(
                nonce,
                ciphertext,
                self._metadata_aad(purpose, lookup_digest),
            )
        except InvalidTag as exc:
            raise OutboxIntegrityError("metadata envelope authentication failed") from exc
        parsed: Any = json.loads(plaintext)
        if not isinstance(parsed, dict):
            raise OutboxIntegrityError("metadata envelope is not a JSON object")
        return parsed

    @staticmethod
    def _metadata_aad(purpose: str, lookup_digest: str) -> bytes:
        return f"meeting-ai-metadata:v1:{purpose}:{lookup_digest}".encode()


class SqliteOutboxStore:
    """Process-safe local durable queue with lease-based delivery ownership."""

    _SCHEMA_VERSION = 5
    _MIGRATIONS: ClassVar[dict[int, str]] = {
        1: "0001_analysis_delivery_outbox.sql",
        2: "0002_ready_event_inbox.sql",
        3: "0003_ready_event_redrive.sql",
        4: "0004_ready_event_inbox_encryption.sql",
        5: "0005_ready_event_lease_fencing.sql",
    }
    _INBOX_SCRUB_MARKER = "ready-inbox-v3-plaintext-scrub"

    def __init__(
        self,
        path: Path,
        cipher: PayloadCipher,
        *,
        max_rows: int,
        busy_timeout_sec: float = 5.0,
    ) -> None:
        if max_rows < 1:
            raise ValueError("max_rows must be positive")
        self.path = path
        self._cipher = cipher
        self._max_rows = max_rows
        self._busy_timeout_ms = max(1, int(busy_timeout_sec * 1000))
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self._busy_timeout_ms / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._restrict_permissions(self.path.parent, 0o700)
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA secure_delete=ON")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version < 0 or version > self._SCHEMA_VERSION:
                raise OutboxError(f"unsupported outbox schema version {version}")
            self._apply_migrations(connection, version)
            self._ensure_store_metadata(connection)
            if not self._inbox_plaintext_scrub_complete(connection):
                self._complete_inbox_plaintext_scrub(connection)
        self._restrict_database_files()
        self.assert_keyring_complete()

    def _apply_migrations(self, connection: sqlite3.Connection, current_version: int) -> None:
        for version in range(current_version + 1, self._SCHEMA_VERSION + 1):
            resource = files("app.migrations").joinpath(self._MIGRATIONS[version])
            try:
                sql = resource.read_text(encoding="utf-8")
            except (FileNotFoundError, OSError) as exc:
                raise OutboxError(f"missing SQLite migration {version}") from exc
            if version == 4:
                self._encrypt_ready_inbox_migration(connection, sql)
                continue
            if version == 5:
                self._lease_fence_ready_inbox_migration(connection, sql)
                continue
            try:
                connection.executescript(
                    f"BEGIN IMMEDIATE;\n{sql}\nPRAGMA user_version={version};\nCOMMIT;"
                )
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise

    def _encrypt_ready_inbox_migration(
        self,
        connection: sqlite3.Connection,
        create_table_sql: str,
    ) -> None:
        """Rebuild the v3 inbox with encrypted identity envelopes."""
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(create_table_sql.strip().rstrip(";"))
            rows = connection.execute(
                "SELECT * FROM meeting_transcript_ready_inbox ORDER BY created_at, event_key"
            ).fetchall()
            for row in rows:
                event_key = str(row["event_key"])
                envelope: dict[str, object] = {
                    "eventKey": event_key,
                    "payloadSha256": str(row["payload_sha256"]),
                    "tenantId": row["tenant_id"],
                    "meetingId": row["meeting_id"],
                    "sessionId": row["session_id"],
                    "finalizationVersion": row["finalization_version"],
                    "analysisRunId": row["analysis_run_id"],
                }
                tenant_id = row["tenant_id"]
                lookup_value = event_key if tenant_id is None else f"{tenant_id}|{event_key}"
                key_id, digest, nonce, ciphertext = self._cipher.encrypt_metadata(
                    purpose="ready-event-inbox",
                    lookup_value=lookup_value,
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
            self._ensure_store_metadata(connection)
            connection.execute(
                """
                INSERT INTO meeting_ai_store_metadata(name, value)
                VALUES (?, 'pending')
                ON CONFLICT(name) DO UPDATE SET value = 'pending'
                """,
                (self._INBOX_SCRUB_MARKER,),
            )
            connection.execute("PRAGMA user_version=4")
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def _lease_fence_ready_inbox_migration(
        self,
        connection: sqlite3.Connection,
        add_column_sql: str,
    ) -> None:
        """Add lease fencing and tenant-bind lookup digests from legacy v4 rows."""
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(add_column_sql.strip().rstrip(";"))
            rows = connection.execute(
                """
                SELECT event_key_digest, identity_key_id, identity_nonce,
                       identity_ciphertext
                FROM meeting_transcript_ready_inbox
                ORDER BY created_at, event_key_digest
                """
            ).fetchall()
            for row in rows:
                old_digest = str(row["event_key_digest"])
                try:
                    envelope = self._cipher.decrypt_metadata(
                        purpose="ready-event-inbox",
                        lookup_digest=old_digest,
                        key_id=str(row["identity_key_id"]),
                        nonce=bytes(row["identity_nonce"]),
                        ciphertext=bytes(row["identity_ciphertext"]),
                    )
                    event_key = envelope["eventKey"]
                    tenant_id = envelope.get("tenantId")
                except (KeyError, TypeError, ValueError, OutboxIntegrityError) as exc:
                    raise OutboxIntegrityError(
                        "ready-event identity cannot be tenant-bound during migration"
                    ) from exc
                if not isinstance(event_key, str) or (
                    tenant_id is not None and not isinstance(tenant_id, str)
                ):
                    raise OutboxIntegrityError(
                        "ready-event identity has invalid tenant binding during migration"
                    )
                lookup_value = event_key if tenant_id is None else f"{tenant_id}|{event_key}"
                key_id, new_digest, nonce, ciphertext = self._cipher.encrypt_metadata(
                    purpose="ready-event-inbox",
                    lookup_value=lookup_value,
                    payload=envelope,
                )
                connection.execute(
                    """
                    UPDATE meeting_transcript_ready_inbox
                    SET event_key_digest = ?, identity_key_id = ?,
                        identity_nonce = ?, identity_ciphertext = ?
                    WHERE event_key_digest = ?
                    """,
                    (new_digest, key_id, nonce, ciphertext, old_digest),
                )
            connection.execute("PRAGMA user_version=5")
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    @staticmethod
    def _ensure_store_metadata(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS meeting_ai_store_metadata (
                name TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )

    def _inbox_plaintext_scrub_complete(self, connection: sqlite3.Connection) -> bool:
        row = connection.execute(
            "SELECT value FROM meeting_ai_store_metadata WHERE name = ?",
            (self._INBOX_SCRUB_MARKER,),
        ).fetchone()
        return row is not None and str(row[0]) == "complete"

    def _complete_inbox_plaintext_scrub(self, connection: sqlite3.Connection) -> None:
        """Finish or resume the v3 plaintext-page scrub before startup succeeds."""
        self._checkpoint_truncate(connection)
        connection.execute("VACUUM")
        self._checkpoint_truncate(connection)
        connection.execute(
            """
            INSERT INTO meeting_ai_store_metadata(name, value)
            VALUES (?, 'complete')
            ON CONFLICT(name) DO UPDATE SET value = 'complete'
            """,
            (self._INBOX_SCRUB_MARKER,),
        )

    @staticmethod
    def _checkpoint_truncate(connection: sqlite3.Connection) -> None:
        result = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if result is None or int(result[0]) != 0:
            raise OutboxError("SQLite WAL truncation is busy; plaintext scrub remains pending")

    @staticmethod
    def _restrict_permissions(path: Path, mode: int) -> None:
        if os.name != "nt" and path.exists():
            path.chmod(mode)

    def _restrict_database_files(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            self._restrict_permissions(Path(f"{self.path}{suffix}"), 0o600)

    def assert_keyring_complete(self) -> None:
        with closing(self._connect()) as connection:
            key_ids = {
                str(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT key_id FROM analysis_delivery_outbox"
                ).fetchall()
            }
            key_ids.update(
                str(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT identity_key_id FROM meeting_transcript_ready_inbox"
                ).fetchall()
            )
        missing = sorted(key_ids - self._cipher.key_ids)
        if missing:
            raise OutboxKeyUnavailableError(
                "outbox contains rows encrypted with unavailable key ids: " + ", ".join(missing)
            )

    def enqueue(
        self,
        *,
        analysis_run_id: str,
        meeting_id: str,
        payload: dict[str, object],
        now: float | None = None,
    ) -> bool:
        """Persist one payload; return False for an exact idempotent replay."""
        timestamp = time.time() if now is None else now
        key_id, nonce, ciphertext, payload_sha256 = self._cipher.encrypt(
            analysis_run_id,
            meeting_id,
            payload,
        )
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                inserted = self.enqueue_in_transaction(
                    connection,
                    analysis_run_id=analysis_run_id,
                    meeting_id=meeting_id,
                    encrypted=(key_id, nonce, ciphertext, payload_sha256),
                    now=timestamp,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        self._restrict_database_files()
        return inserted

    def enqueue_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        analysis_run_id: str,
        meeting_id: str,
        payload: dict[str, object] | None = None,
        encrypted: tuple[str, bytes, bytes, str] | None = None,
        now: float,
    ) -> bool:
        """Insert through an existing write transaction.

        The ready-event inbox uses this method so the encrypted result row and
        its ``OUTBOXED`` transition become one SQLite commit.
        """
        if (payload is None) == (encrypted is None):
            raise ValueError("exactly one of payload or encrypted must be supplied")
        if encrypted is None:
            assert payload is not None
            encrypted = self._cipher.encrypt(analysis_run_id, meeting_id, payload)
        key_id, nonce, ciphertext, payload_sha256 = encrypted
        existing = connection.execute(
            """
            SELECT meeting_id, payload_sha256
            FROM analysis_delivery_outbox
            WHERE analysis_run_id = ?
            """,
            (analysis_run_id,),
        ).fetchone()
        if existing is not None:
            if (
                str(existing["meeting_id"]) != meeting_id
                or str(existing["payload_sha256"]) != payload_sha256
            ):
                raise OutboxConflictError("analysis_run_id already exists with a different payload")
            return False

        row_count = int(
            connection.execute("SELECT COUNT(*) FROM analysis_delivery_outbox").fetchone()[0]
        )
        if row_count >= self._max_rows:
            raise OutboxFullError("durable analysis delivery queue is full")
        connection.execute(
            """
            INSERT INTO analysis_delivery_outbox (
                analysis_run_id, meeting_id, key_id, nonce, ciphertext,
                payload_sha256, state, attempt_count, next_attempt_at,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'PENDING', 0, ?, ?, ?)
            """,
            (
                analysis_run_id,
                meeting_id,
                key_id,
                nonce,
                ciphertext,
                payload_sha256,
                now,
                now,
                now,
            ),
        )
        return True

    def claim_next(
        self,
        *,
        owner: str,
        lease_sec: float,
        now: float | None = None,
    ) -> ClaimedMessage | None:
        timestamp = time.time() if now is None else now
        lease_until = timestamp + lease_sec
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT *
                    FROM analysis_delivery_outbox
                    WHERE (state = 'PENDING' AND next_attempt_at <= ?)
                       OR (state = 'IN_FLIGHT' AND lease_until <= ?)
                    ORDER BY created_at, analysis_run_id
                    LIMIT 1
                    """,
                    (timestamp, timestamp),
                ).fetchone()
                if row is None:
                    connection.commit()
                    return None
                attempt_count = int(row["attempt_count"]) + 1
                connection.execute(
                    """
                    UPDATE analysis_delivery_outbox
                    SET state = 'IN_FLIGHT', attempt_count = ?, lease_owner = ?,
                        lease_until = ?, updated_at = ?
                    WHERE analysis_run_id = ?
                    """,
                    (
                        attempt_count,
                        owner,
                        lease_until,
                        timestamp,
                        str(row["analysis_run_id"]),
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

        try:
            payload = self._cipher.decrypt(
                key_id=str(row["key_id"]),
                nonce=bytes(row["nonce"]),
                ciphertext=bytes(row["ciphertext"]),
                analysis_run_id=str(row["analysis_run_id"]),
                meeting_id=str(row["meeting_id"]),
            )
        except (OutboxIntegrityError, OutboxKeyUnavailableError) as exc:
            self.mark_dead(
                analysis_run_id=str(row["analysis_run_id"]),
                owner=owner,
                error_code=type(exc).__name__,
                now=timestamp,
            )
            raise
        return ClaimedMessage(
            analysis_run_id=str(row["analysis_run_id"]),
            meeting_id=str(row["meeting_id"]),
            payload=payload,
            attempt_count=attempt_count,
            created_at=float(row["created_at"]),
        )

    def mark_delivered(self, *, analysis_run_id: str, owner: str) -> bool:
        """Delete sensitive local payload after canonical acceptance/replay."""
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                DELETE FROM analysis_delivery_outbox
                WHERE analysis_run_id = ? AND state = 'IN_FLIGHT' AND lease_owner = ?
                """,
                (analysis_run_id, owner),
            )
        return cursor.rowcount == 1

    def mark_retry(
        self,
        *,
        analysis_run_id: str,
        owner: str,
        next_attempt_at: float,
        error_code: str,
        now: float | None = None,
    ) -> bool:
        timestamp = time.time() if now is None else now
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE analysis_delivery_outbox
                SET state = 'PENDING', next_attempt_at = ?, lease_owner = NULL,
                    lease_until = NULL, updated_at = ?, last_error_code = ?
                WHERE analysis_run_id = ? AND state = 'IN_FLIGHT' AND lease_owner = ?
                """,
                (next_attempt_at, timestamp, error_code[:128], analysis_run_id, owner),
            )
        return cursor.rowcount == 1

    def mark_dead(
        self,
        *,
        analysis_run_id: str,
        owner: str,
        error_code: str,
        now: float | None = None,
    ) -> bool:
        timestamp = time.time() if now is None else now
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE analysis_delivery_outbox
                SET state = 'DEAD', lease_owner = NULL, lease_until = NULL,
                    updated_at = ?, last_error_code = ?
                WHERE analysis_run_id = ? AND state = 'IN_FLIGHT' AND lease_owner = ?
                """,
                (timestamp, error_code[:128], analysis_run_id, owner),
            )
        return cursor.rowcount == 1

    def requeue_dead(self, analysis_run_id: str, *, now: float | None = None) -> bool:
        timestamp = time.time() if now is None else now
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE analysis_delivery_outbox
                SET state = 'PENDING', attempt_count = 0, next_attempt_at = ?,
                    updated_at = ?, last_error_code = NULL
                WHERE analysis_run_id = ? AND state = 'DEAD'
                """,
                (timestamp, timestamp, analysis_run_id),
            )
        return cursor.rowcount == 1

    def list_dead(self, *, limit: int = 100) -> list[DeadLetterMetadata]:
        """List bounded DLQ metadata without selecting or decrypting payloads."""
        if not 1 <= limit <= 1000:
            raise ValueError("dead-letter list limit must be between 1 and 1000")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT analysis_run_id, attempt_count, created_at, updated_at,
                       last_error_code
                FROM analysis_delivery_outbox
                WHERE state = 'DEAD'
                ORDER BY updated_at DESC, analysis_run_id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            DeadLetterMetadata(
                analysis_run_id=str(row["analysis_run_id"]),
                attempt_count=int(row["attempt_count"]),
                created_at=float(row["created_at"]),
                updated_at=float(row["updated_at"]),
                last_error_code=(
                    None if row["last_error_code"] is None else str(row["last_error_code"])
                ),
            )
            for row in rows
        ]

    def summary(self, *, now: float | None = None) -> OutboxSummary:
        timestamp = time.time() if now is None else now
        with closing(self._connect()) as connection:
            counts = {
                str(row["state"]): int(row["count"])
                for row in connection.execute(
                    "SELECT state, COUNT(*) AS count FROM analysis_delivery_outbox GROUP BY state"
                ).fetchall()
            }
            oldest = connection.execute(
                """
                SELECT MIN(created_at)
                FROM analysis_delivery_outbox
                WHERE state IN ('PENDING', 'IN_FLIGHT')
                """
            ).fetchone()[0]
        return OutboxSummary(
            pending=counts.get(OutboxState.PENDING.value, 0),
            in_flight=counts.get(OutboxState.IN_FLIGHT.value, 0),
            dead=counts.get(OutboxState.DEAD.value, 0),
            oldest_pending_age_sec=None if oldest is None else max(0.0, timestamp - float(oldest)),
        )

    def has_capacity(self) -> bool:
        """Return whether a new result identity can be accepted."""
        with closing(self._connect()) as connection:
            row_count = int(
                connection.execute("SELECT COUNT(*) FROM analysis_delivery_outbox").fetchone()[0]
            )
        return row_count < self._max_rows
