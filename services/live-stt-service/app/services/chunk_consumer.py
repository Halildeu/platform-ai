"""Redis Streams audio-chunk metadata consumer (PR-stt-04, #137).

Consumes the gateway dispatcher's partitioned streams
(``audio:chunks:p00..p31``, ADR-0031 D3) with consumer group
``live-stt-v1`` and feeds validated chunk envelopes to a pluggable
handler. Producer contract is fixed by platform-backend PR #534:

- Stream key: ``audio:chunks:p<NN>``, ``NN = hash(tenantId + sessionId) %
  partition-count`` (zero-padded) — all chunks of one session land on one
  partition, so dedup state can stay partition-local.
- Payload is **hash-only** (SHA-256 + routing metadata, never raw audio) —
  ADR-0030 §"Cross-Server STT Transit Boundary" PII boundary.
- Replay-safe dedup MUST use the ``messageId`` field
  (``sessionId:chunkSeq``), NOT the Redis entry ID (entry IDs are
  Redis-assigned timestamps and carry no session/chunk identity).
- XACK + bounded trim are consumer responsibility (the producer never
  trims — unread backlog is its backpressure signal, and it watches
  XPENDING for lag, so prompt XACK keeps the gateway accepting chunks).
- Crash/restart recovery claims stale pending entries via XAUTOCLAIM.

STT pipeline integration (resolving the audio bytes referenced by the
hash and scheduling transcription) is the follow-up slice; this slice
delivers transport, validation, dedup, ack/trim discipline and metrics
behind the ``ChunkHandler`` seam.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from typing import Any, Protocol

from prometheus_client import Counter, Gauge
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.config import Settings

logger = logging.getLogger(__name__)

# ── Prometheus metrics (exposed by the existing /metrics endpoint) ────────────

stt_chunk_consumer_up = Gauge(
    "stt_chunk_consumer_up",
    "1 while the Redis Streams chunk consumer loop is running",
)
stt_chunks_consumed_total = Counter(
    "stt_chunks_consumed_total",
    "Audio chunk envelopes consumed from the gateway streams",
    ["result"],  # success | duplicate | invalid | retry
)
stt_chunks_claimed_total = Counter(
    "stt_chunks_claimed_total",
    "Stale pending entries claimed via XAUTOCLAIM after crash/restart",
)


class ChunkEnvelope(BaseModel):
    """Validated gateway chunk metadata (producer streamFields contract).

    Hash-only payload: ``sha256`` references the audio content; raw bytes
    never transit this stream (ADR-0030 PII boundary).
    """

    model_config = ConfigDict(populate_by_name=True)

    message_id: str = Field(alias="messageId", min_length=1)
    session_id: str = Field(alias="sessionId", min_length=1)
    chunk_seq: int = Field(alias="chunkSeq", ge=0)
    tenant_id: str = Field(alias="tenantId", min_length=1)
    sha256: str = Field(min_length=1)
    user_id: str = Field(alias="userId", default="")
    meeting_id: str = Field(alias="meetingId", default="")
    device_id: str = Field(alias="deviceId", default="")
    language: str = Field(default="")
    audio_format: str = Field(alias="audioFormat", default="")
    sample_rate_hz: int = Field(alias="sampleRateHz", default=0, ge=0)
    channels: int = Field(default=0, ge=0)
    chunk_started_at_ms: int = Field(alias="chunkStartedAtMs", default=0, ge=0)
    correlation_id: str = Field(alias="correlationId", default="")
    length: int = Field(default=0, ge=0)


class ChunkHandler(Protocol):
    """Seam for the STT pipeline integration (follow-up slice)."""

    def handle(self, envelope: ChunkEnvelope) -> None: ...


class LoggingChunkHandler:
    """Default handler: PII-safe structured log (ids + hash prefix only)."""

    def handle(self, envelope: ChunkEnvelope) -> None:
        logger.info(
            "chunk_envelope_received",
            extra={
                "correlation_id": envelope.correlation_id or "-",
                "session_id": envelope.session_id,
                "chunk_seq": envelope.chunk_seq,
                "sha256_prefix": envelope.sha256[:8],
            },
        )


class RedisStreamsClient(Protocol):
    """Subset of redis-py used by the consumer (test seam)."""

    def xgroup_create(
        self, name: str, groupname: str, id: str, mkstream: bool  # noqa: A002
    ) -> object: ...

    def xreadgroup(
        self, **kwargs: object
    ) -> list[tuple[object, list[tuple[object, dict[object, object]]]]]: ...

    def xack(self, name: str, groupname: str, *ids: str) -> object: ...

    def xautoclaim(
        self,
        name: str,
        groupname: str,
        consumername: str,
        min_idle_time: int,
        start_id: str,
        count: int,
    ) -> object: ...

    def xtrim(self, name: str, maxlen: int, approximate: bool) -> object: ...


def _decode(value: object) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def _decode_fields(fields: dict[object, object]) -> dict[str, str]:
    return {_decode(key): _decode(value) for key, value in fields.items()}


class _LruSeenSet:
    """Bounded replay-dedup memory keyed by messageId (sessionId:chunkSeq)."""

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._entries: OrderedDict[str, None] = OrderedDict()

    def seen(self, key: str) -> bool:
        if key in self._entries:
            self._entries.move_to_end(key)
            return True
        return False

    def add(self, key: str) -> None:
        self._entries[key] = None
        self._entries.move_to_end(key)
        while len(self._entries) > self._capacity:
            self._entries.popitem(last=False)


class AudioChunkConsumer:
    """XREADGROUP loop over the 32 gateway partitions.

    Ack discipline (producer XPENDING contract): success, duplicate and
    invalid (poison) messages are XACK'ed; only unexpected handler
    failures stay pending so XAUTOCLAIM can retry them after
    ``chunk_claim_idle_ms``.
    """

    def __init__(
        self,
        settings: Settings,
        redis_client: RedisStreamsClient,
        handler: ChunkHandler,
    ) -> None:
        self._settings = settings
        self._redis = redis_client
        self._handler = handler
        self._stop_event = threading.Event()
        self._seen = _LruSeenSet(settings.chunk_dedup_cache_size)

    # ── topology ──────────────────────────────────────────────────────────────

    def partition_keys(self) -> list[str]:
        prefix = self._settings.chunk_stream_prefix
        return [f"{prefix}{index:02d}" for index in range(self._settings.chunk_partition_count)]

    def ensure_groups(self) -> None:
        """XGROUP CREATE (mkstream) on every partition; BUSYGROUP is fine."""
        for key in self.partition_keys():
            try:
                self._redis.xgroup_create(
                    name=key,
                    groupname=self._settings.chunk_consumer_group,
                    id="0",
                    mkstream=True,
                )
            except Exception as exc:  # redis-py raises plain ResponseError
                if "BUSYGROUP" not in str(exc):
                    raise

    def stop(self) -> None:
        self._stop_event.set()

    # ── message processing ────────────────────────────────────────────────────

    def _ack(self, stream_key: str, entry_id: str) -> None:
        self._redis.xack(stream_key, self._settings.chunk_consumer_group, entry_id)

    def process_message(
        self, stream_key: str, entry_id: str, fields: dict[object, object]
    ) -> None:
        try:
            envelope = ChunkEnvelope.model_validate(_decode_fields(fields))
        except (ValidationError, ValueError) as exc:
            # Poison message: ack so it never blocks the partition, keep a trace.
            stt_chunks_consumed_total.labels(result="invalid").inc()
            self._ack(stream_key, entry_id)
            logger.warning(
                "chunk_envelope_invalid",
                extra={
                    "correlation_id": "-",
                    "stream_key": stream_key,
                    "error_class": type(exc).__name__,
                },
            )
            return

        if self._seen.seen(envelope.message_id):
            # Replay (producer retry / claim race): contract says dedup on
            # messageId, NOT entry id — ack the duplicate and move on.
            stt_chunks_consumed_total.labels(result="duplicate").inc()
            self._ack(stream_key, entry_id)
            return

        try:
            self._handler.handle(envelope)
        except Exception:
            # NOT acked: stays pending; retried via XAUTOCLAIM after idle.
            stt_chunks_consumed_total.labels(result="retry").inc()
            logger.exception(
                "chunk_handler_failed_pending",
                extra={"correlation_id": envelope.correlation_id or "-"},
            )
            return

        self._seen.add(envelope.message_id)
        self._ack(stream_key, entry_id)
        stt_chunks_consumed_total.labels(result="success").inc()

    # ── crash/restart recovery + trim ─────────────────────────────────────────

    def claim_stale(self) -> int:
        """XAUTOCLAIM entries idle beyond chunk_claim_idle_ms, reprocess them."""
        claimed = 0
        for key in self.partition_keys():
            try:
                response: Any = self._redis.xautoclaim(
                    name=key,
                    groupname=self._settings.chunk_consumer_group,
                    consumername=self._settings.chunk_consumer_name,
                    min_idle_time=self._settings.chunk_claim_idle_ms,
                    start_id="0-0",
                    count=self._settings.chunk_batch_size,
                )
            except Exception as exc:  # redis-py raises plain ResponseError
                if "NOGROUP" in str(exc):
                    continue
                raise
            messages = response[1] if isinstance(response, list | tuple) else []
            for entry_id, fields in messages:
                claimed += 1
                stt_chunks_claimed_total.inc()
                self.process_message(key, _decode(entry_id), fields)
        return claimed

    def trim(self, stream_key: str) -> None:
        """Bounded trim (consumer responsibility — producer never trims)."""
        self._redis.xtrim(
            stream_key,
            maxlen=self._settings.chunk_trim_maxlen,
            approximate=True,
        )

    # ── main loop ─────────────────────────────────────────────────────────────

    def run(self) -> None:
        self.ensure_groups()
        stt_chunk_consumer_up.set(1)
        loops_since_claim = 0
        try:
            while not self._stop_event.is_set():
                records = self._redis.xreadgroup(
                    groupname=self._settings.chunk_consumer_group,
                    consumername=self._settings.chunk_consumer_name,
                    streams=dict.fromkeys(self.partition_keys(), ">"),
                    count=self._settings.chunk_batch_size,
                    block=self._settings.chunk_block_ms,
                )
                touched: list[str] = []
                for stream, messages in records or []:
                    stream_key = _decode(stream)
                    touched.append(stream_key)
                    for entry_id, fields in messages:
                        self.process_message(stream_key, _decode(entry_id), fields)
                for stream_key in touched:
                    self.trim(stream_key)
                loops_since_claim += 1
                if loops_since_claim >= self._settings.chunk_claim_every_loops:
                    loops_since_claim = 0
                    self.claim_stale()
        finally:
            stt_chunk_consumer_up.set(0)


def build_redis_client(settings: Settings) -> Any:
    """Real redis-py client (lazy import keeps unit tests dependency-free)."""
    from redis import Redis

    return Redis.from_url(settings.redis_url, decode_responses=False)
