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
import json
import os
import secrets
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

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


class SqliteOutboxStore:
    """Process-safe local durable queue with lease-based delivery ownership."""

    _SCHEMA_VERSION = 1

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
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS analysis_delivery_outbox (
                    analysis_run_id TEXT PRIMARY KEY,
                    meeting_id TEXT NOT NULL,
                    key_id TEXT NOT NULL,
                    nonce BLOB NOT NULL,
                    ciphertext BLOB NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('PENDING', 'IN_FLIGHT', 'DEAD')),
                    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
                    next_attempt_at REAL NOT NULL,
                    lease_owner TEXT,
                    lease_until REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    last_error_code TEXT,
                    CHECK (
                        (state = 'IN_FLIGHT'
                            AND lease_owner IS NOT NULL
                            AND lease_until IS NOT NULL)
                        OR state != 'IN_FLIGHT'
                    )
                );
                CREATE INDEX IF NOT EXISTS idx_analysis_delivery_due
                    ON analysis_delivery_outbox(state, next_attempt_at, lease_until, created_at);
                """
            )
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version not in (0, self._SCHEMA_VERSION):
                raise OutboxError(f"unsupported outbox schema version {version}")
            connection.execute(f"PRAGMA user_version={self._SCHEMA_VERSION}")
        self._restrict_database_files()
        self.assert_keyring_complete()

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
                        raise OutboxConflictError(
                            "analysis_run_id already exists with a different payload"
                        )
                    connection.commit()
                    return False

                row_count = int(
                    connection.execute("SELECT COUNT(*) FROM analysis_delivery_outbox").fetchone()[
                        0
                    ]
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
                        timestamp,
                        timestamp,
                        timestamp,
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        self._restrict_database_files()
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
