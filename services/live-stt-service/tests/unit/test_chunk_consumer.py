"""PR-stt-04 (#137) — gateway Redis Streams chunk consumer unit tests.

Producer contract (platform-backend PR #534): partitioned streams
audio:chunks:p00..p31, group live-stt-v1, messageId dedup (NOT entry id),
XACK + bounded trim on the consumer, XAUTOCLAIM crash recovery,
hash-only PII boundary.
"""

from __future__ import annotations

from app.core.config import Settings
from app.services.chunk_consumer import (
    AudioChunkConsumer,
    ChunkEnvelope,
    CoordinationChunkHandler,
)


def make_settings(**overrides: object) -> Settings:
    return Settings(chunk_consumer_name="test-consumer", **overrides)  # type: ignore[arg-type]


def valid_fields(message_id: str = "sess-1:3") -> dict[object, object]:
    """Producer streamFields payload — hash + routing metadata, no audio."""
    return {
        b"messageId": message_id.encode(),
        b"sessionId": b"sess-1",
        b"chunkSeq": b"3",
        b"tenantId": b"42",
        b"userId": b"7",
        b"meetingId": b"meeting-1",
        b"deviceId": b"dev-1",
        b"language": b"tr",
        b"audioFormat": b"PCM16",
        b"sampleRateHz": b"16000",
        b"channels": b"1",
        b"chunkStartedAtMs": b"1000",
        b"correlationId": b"corr-1",
        b"sha256": b"abc123hash",
        b"length": b"3",
    }


class FakeRedis:
    def __init__(self) -> None:
        self.groups: list[tuple[str, str]] = []
        self.acked: list[tuple[str, str]] = []
        self.trimmed: list[tuple[str, int]] = []
        self.autoclaim_response: object = ("0-0", [], [])
        self.read_batches: list[list[tuple[bytes, list[tuple[bytes, dict[object, object]]]]]] = []
        self.busygroup = False

    def xgroup_create(
        self, name: str, groupname: str, id: str, mkstream: bool  # noqa: A002
    ) -> object:
        if self.busygroup:
            raise RuntimeError("BUSYGROUP Consumer Group name already exists")
        self.groups.append((name, groupname))
        return True

    def xreadgroup(
        self, **_kwargs: object
    ) -> list[tuple[object, list[tuple[object, dict[object, object]]]]]:
        if self.read_batches:
            return self.read_batches.pop(0)  # type: ignore[return-value]
        return []

    def xack(self, name: str, _groupname: str, *ids: str) -> object:
        self.acked.extend((name, entry_id) for entry_id in ids)
        return len(ids)

    def xautoclaim(self, **_kwargs: object) -> object:
        return self.autoclaim_response

    def xtrim(self, name: str, maxlen: int, approximate: bool) -> object:
        self.trimmed.append((name, maxlen))
        return 0


class RecordingHandler:
    def __init__(self) -> None:
        self.envelopes: list[ChunkEnvelope] = []

    def handle(self, envelope: ChunkEnvelope) -> None:
        self.envelopes.append(envelope)


class ExplodingHandler:
    def handle(self, envelope: ChunkEnvelope) -> None:
        raise RuntimeError("pipeline unavailable")


def make_consumer(
    redis: FakeRedis | None = None,
    handler: RecordingHandler | ExplodingHandler | CoordinationChunkHandler | None = None,
    **overrides: object,
) -> tuple[AudioChunkConsumer, FakeRedis, RecordingHandler]:
    redis = redis or FakeRedis()
    recording = handler or RecordingHandler()
    consumer = AudioChunkConsumer(make_settings(**overrides), redis, recording)  # type: ignore[arg-type]
    return consumer, redis, recording  # type: ignore[return-value]


# ── topology ───────────────────────────────────────────────────────────────────


def test_partition_keys_match_producer_format() -> None:
    consumer, _redis, _handler = make_consumer()
    keys = consumer.partition_keys()
    assert len(keys) == 32
    assert keys[0] == "audio:chunks:p00"
    assert keys[7] == "audio:chunks:p07"
    assert keys[31] == "audio:chunks:p31"


def test_ensure_groups_creates_group_on_every_partition() -> None:
    consumer, redis, _handler = make_consumer()
    consumer.ensure_groups()
    assert len(redis.groups) == 32
    assert all(group == "live-stt-v1" for _key, group in redis.groups)


def test_ensure_groups_tolerates_busygroup() -> None:
    redis = FakeRedis()
    redis.busygroup = True
    consumer, _redis, _handler = make_consumer(redis=redis)
    consumer.ensure_groups()  # must not raise


# ── message processing ─────────────────────────────────────────────────────────


def test_valid_message_is_handled_then_acked() -> None:
    consumer, redis, handler = make_consumer()
    consumer.process_message("audio:chunks:p07", "1-0", valid_fields())
    assert len(handler.envelopes) == 1
    envelope = handler.envelopes[0]
    assert envelope.message_id == "sess-1:3"
    assert envelope.session_id == "sess-1"
    assert envelope.chunk_seq == 3
    assert envelope.sha256 == "abc123hash"
    assert redis.acked == [("audio:chunks:p07", "1-0")]


def test_duplicate_message_id_is_acked_but_not_rehandled() -> None:
    """Dedup keys on messageId (sessionId:chunkSeq), NOT the Redis entry id."""
    consumer, redis, handler = make_consumer()
    consumer.process_message("audio:chunks:p07", "1-0", valid_fields())
    # Same messageId arrives again under a DIFFERENT entry id (producer retry).
    consumer.process_message("audio:chunks:p07", "9-9", valid_fields())
    assert len(handler.envelopes) == 1
    assert ("audio:chunks:p07", "9-9") in redis.acked


def test_invalid_message_is_acked_as_poison() -> None:
    consumer, redis, handler = make_consumer()
    consumer.process_message("audio:chunks:p00", "2-0", {b"sessionId": b"only"})
    assert handler.envelopes == []
    assert redis.acked == [("audio:chunks:p00", "2-0")]


def test_handler_failure_keeps_message_pending_for_retry() -> None:
    consumer, redis, _handler = make_consumer(handler=ExplodingHandler())
    consumer.process_message("audio:chunks:p00", "3-0", valid_fields())
    assert redis.acked == []  # stays pending → XAUTOCLAIM retries later


def test_failed_message_is_not_marked_seen_so_retry_succeeds() -> None:
    redis = FakeRedis()
    consumer, _redis, _handler = make_consumer(redis=redis, handler=ExplodingHandler())
    consumer.process_message("audio:chunks:p00", "4-0", valid_fields("sess-1:9"))
    # Swap in a healthy handler (recovery) and replay the same messageId.
    recovered = RecordingHandler()
    consumer._handler = recovered  # test seam: swap handler after failure
    consumer.process_message("audio:chunks:p00", "4-0", valid_fields("sess-1:9"))
    assert len(recovered.envelopes) == 1
    assert redis.acked == [("audio:chunks:p00", "4-0")]


# ── crash/restart recovery + trim ──────────────────────────────────────────────


def test_claim_stale_processes_and_acks_claimed_entries() -> None:
    redis = FakeRedis()
    redis.autoclaim_response = (b"0-0", [(b"5-0", valid_fields("sess-1:5"))], [])
    consumer, _redis, handler = make_consumer(redis=redis)
    claimed = consumer.claim_stale()
    # One claimed entry per partition probe (same fake response for all 32) —
    # the FIRST is processed, the remaining 31 are deduped by messageId.
    assert claimed == 32
    assert len(handler.envelopes) == 1
    assert len(redis.acked) == 32


def test_trim_is_bounded_and_approximate() -> None:
    consumer, redis, _handler = make_consumer()
    consumer.trim("audio:chunks:p01")
    assert redis.trimmed == [("audio:chunks:p01", 10_000)]


# ── run loop ───────────────────────────────────────────────────────────────────


def test_run_processes_batch_trims_then_stops() -> None:
    redis = FakeRedis()
    redis.read_batches = [
        [(b"audio:chunks:p07", [(b"6-0", valid_fields("sess-1:6"))])],
    ]
    consumer, _redis, handler = make_consumer(redis=redis)

    original_read = redis.xreadgroup

    def reading_then_stop(**kwargs: object) -> list:  # type: ignore[type-arg]
        result = original_read(**kwargs)
        if not result:
            consumer.stop()
        return result

    redis.xreadgroup = reading_then_stop  # type: ignore[method-assign]
    consumer.run()
    assert len(handler.envelopes) == 1
    assert ("audio:chunks:p07", "6-0") in redis.acked
    assert ("audio:chunks:p07", 10_000) in redis.trimmed


# ── config surface ─────────────────────────────────────────────────────────────


def test_consumer_disabled_by_default() -> None:
    assert make_settings().chunk_consumer_enabled is False


def test_contract_defaults_match_adr_0031_d3() -> None:
    settings = make_settings()
    assert settings.chunk_stream_prefix == "audio:chunks:p"
    assert settings.chunk_partition_count == 32
    assert settings.chunk_consumer_group == "live-stt-v1"


def test_default_handler_is_control_plane_only(caplog) -> None:
    handler = CoordinationChunkHandler()
    envelope = ChunkEnvelope.model_validate(
        {
            "messageId": "sess-1:3",
            "sessionId": "sess-1",
            "chunkSeq": 3,
            "tenantId": "42",
            "sha256": "abc123hash",
            "correlationId": "corr-1",
        }
    )

    with caplog.at_level("INFO"):
        handler.handle(envelope)

    assert "chunk_control_plane_envelope_received" in caplog.text
    record = next(
        item for item in caplog.records if item.message == "chunk_control_plane_envelope_received"
    )
    assert record.sha256_prefix == "abc123ha"
    assert record.sha256_prefix != envelope.sha256
