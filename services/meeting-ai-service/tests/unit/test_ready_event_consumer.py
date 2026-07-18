"""End-to-end unit tests for Redis PEL -> inbox -> encrypted outbox."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import random
import sqlite3
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.services.analysis_application import AnalysisApplicationService
from app.services.analysis_delivery import AnalysisDeliveryRuntime
from app.services.analyze import MeetingAnalysisService
from app.services.canonical_transcript_client import (
    CanonicalTranscriptRetryableError,
    CanonicalTranscriptSnapshot,
)
from app.services.durable_outbox import PayloadCipher
from app.services.meeting_service_client import DeliveryAttempt, DeliveryDisposition
from app.services.ready_event_consumer import ReadyEventConsumerRuntime, _build_redis_client
from app.services.ready_event_inbox import ReadyInboxLeaseLostError

MEETING = "11111111-1111-4111-8111-111111111111"
SESSION = "22222222-2222-4222-8222-222222222222"
TENANT = "33333333-3333-4333-8333-333333333333"
EVENT_KEY = f"meeting.transcript|{SESSION}|meeting.transcript.ready|1"
RAW_TRANSCRIPT = "RAW-CANONICAL-TRANSCRIPT-SECRET Bütçe kararlaştırıldı."


def _event_lookup_digest() -> str:
    return PayloadCipher({"v1": b"K" * 32}, "v1").lookup_digests(
        purpose="ready-event-inbox",
        value=EVENT_KEY,
    )[0]


class FakeRedis:
    def __init__(self) -> None:
        self.acked: list[tuple[object, ...]] = []
        self.added: list[dict[str, object]] = []
        self.autoclaim_result: object = ("0-0", [], [])
        self.autoclaim_start_ids: list[object] = []
        self.readgroup_results: list[object] = []
        self.readgroup_calls: list[dict[str, object]] = []

    async def xgroup_create(self, **kwargs: object) -> object:
        return True

    async def xreadgroup(self, **kwargs: object) -> object:
        self.readgroup_calls.append(dict(kwargs))
        return self.readgroup_results.pop(0) if self.readgroup_results else []

    async def xautoclaim(self, **kwargs: object) -> object:
        self.autoclaim_start_ids.append(kwargs["start_id"])
        return self.autoclaim_result

    async def xadd(self, **kwargs: object) -> object:
        self.added.append(dict(kwargs))
        return "1-0"

    async def xack(self, *args: object) -> object:
        self.acked.append(args)
        return 1

    async def aclose(self) -> None:
        return None


class FakeDeliveryTransport:
    async def deliver(self, message):  # type: ignore[no-untyped-def]
        return DeliveryAttempt(DeliveryDisposition.DELIVERED)


class FakeTranscriptClient:
    def __init__(self, *, retry: bool = False) -> None:
        self.calls = 0
        self.retry = retry

    async def fetch(self, event):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.retry:
            raise CanonicalTranscriptRetryableError("transcript_http_503")
        return CanonicalTranscriptSnapshot(
            tenantId=str(event.tenant_id),
            meetingId=str(event.meeting_id),
            sessionId=str(event.session_id),
            finalizationVersion=event.finalization_version,
            state="FINALIZED",
            transcript=RAW_TRANSCRIPT,
            transcriptSha256=hashlib.sha256(RAW_TRANSCRIPT.encode()).hexdigest(),
            segmentCount=event.segment_count,
            segments=[{"text": RAW_TRANSCRIPT, "start": 0.0, "end": 4.0}],
        )

    async def aclose(self) -> None:
        return None


class BlockingTranscriptClient(FakeTranscriptClient):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def fetch(self, event):  # type: ignore[no-untyped-def]
        self.started.set()
        await self.release.wait()
        return await super().fetch(event)


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    key = base64.b64encode(b"K" * 32).decode()
    values: dict[str, object] = {
        "ingestion_enabled": True,
        "meeting_service_base_url": "https://meeting.test",
        "meeting_service_token_url": "https://auth.test/token",
        "meeting_service_client_id": "meeting-ai",
        "meeting_service_client_secret": SecretStr("meeting-secret"),
        "ingestion_store_path": tmp_path / "delivery.sqlite3",
        "ingestion_active_key_id": "v1",
        "ingestion_encryption_keys_json": SecretStr(json.dumps({"v1": key})),
        "ready_consumer_enabled": True,
        "ready_producer_replay_horizon_sec": 604_800.0,
        "ready_redis_url": SecretStr("redis://redis.test:6379/0"),
        "ready_consumer_jitter_ratio": 0.0,
        "transcript_service_base_url": "https://transcript.test",
        "transcript_service_snapshot_path_template": (
            "/api/v1/internal/tenants/{tenant_id}/meetings/{meeting_id}"
            "/sessions/{session_id}/finalizations/{finalization_version}"
        ),
        "transcript_service_token_url": "https://auth.test/token",
        "transcript_service_client_id": "meeting-ai-ready",
        "transcript_service_client_secret": SecretStr("transcript-secret"),
    }
    values.update(overrides)
    return Settings(**values)


def _fields(*, generated_at: str = "2026-07-18T01:02:03Z") -> dict[object, object]:
    payload = json.dumps(
        {
            "schema": "meeting.event.v1",
            "eventType": "meeting.transcript.ready",
            "analysisRunId": None,
            "meetingId": MEETING,
            "tenantId": TENANT,
            "orgId": TENANT,
            "generatedAt": generated_at,
            "transcriptSessionId": SESSION,
            "finalizationVersion": 1,
            "segmentCount": 1,
        },
        separators=(",", ":"),
    ).encode()
    return {
        b"eventKey": EVENT_KEY.encode(),
        b"eventType": b"meeting.transcript.ready",
        b"aggregateId": SESSION.encode(),
        b"meetingId": MEETING.encode(),
        b"tenantId": TENANT.encode(),
        b"orgId": TENANT.encode(),
        b"payload": payload,
    }


def test_redis_client_has_bounded_connect_and_command_timeouts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class StubRedis:
        @classmethod
        def from_url(cls, url: str, **kwargs: object) -> object:
            captured["url"] = url
            captured.update(kwargs)
            return object()

    redis_package = ModuleType("redis")
    redis_asyncio = ModuleType("redis.asyncio")
    redis_asyncio.Redis = StubRedis  # type: ignore[attr-defined]
    redis_package.asyncio = redis_asyncio  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "redis", redis_package)
    monkeypatch.setitem(sys.modules, "redis.asyncio", redis_asyncio)

    settings = _settings(
        tmp_path,
        ready_redis_connect_timeout_sec=3.0,
        ready_redis_command_timeout_sec=7.0,
    )
    _build_redis_client(settings)
    assert captured["url"] == "redis://redis.test:6379/0"
    assert captured["socket_connect_timeout"] == 3.0
    assert captured["socket_timeout"] == 7.0
    assert captured["health_check_interval"] == 30
    assert captured["retry_on_timeout"] is False


def _runtime(
    tmp_path: Path,
    *,
    transcript_client: FakeTranscriptClient | None = None,
    settings_overrides: dict[str, object] | None = None,
) -> tuple[ReadyEventConsumerRuntime, AnalysisDeliveryRuntime, FakeRedis, FakeTranscriptClient]:
    settings = _settings(tmp_path, **(settings_overrides or {}))
    delivery = AnalysisDeliveryRuntime(settings, transport=FakeDeliveryTransport())
    application = AnalysisApplicationService(settings, MeetingAnalysisService(settings))
    redis = FakeRedis()
    transcripts = transcript_client or FakeTranscriptClient()
    runtime = ReadyEventConsumerRuntime(
        settings,
        application,
        delivery,
        redis_client=redis,
        transcript_client=transcripts,
        random_source=random.Random(0),  # noqa: S311 - deterministic retry jitter test
    )
    return runtime, delivery, redis, transcripts


def test_ready_event_outboxes_once_then_duplicate_only_acks(tmp_path: Path) -> None:
    async def scenario() -> tuple[int, int, int, bytes]:
        runtime, delivery, redis, transcripts = _runtime(tmp_path)
        await runtime.process_message("1-0", _fields())
        assert delivery.store is not None
        first_pending = delivery.store.summary().pending
        await runtime.process_message("2-0", _fields())
        database_bytes = _settings(tmp_path).ingestion_store_path.read_bytes()
        health = await runtime.health()
        durable_bytes = database_bytes + repr(health).encode()
        return transcripts.calls, len(redis.acked), first_pending, durable_bytes

    calls, ack_count, pending, durable_bytes = asyncio.run(scenario())
    assert calls == 1
    assert ack_count == 2
    assert pending == 1
    assert RAW_TRANSCRIPT.encode() not in durable_bytes


def test_redis_message_stays_pending_until_analysis_outbox_commit(tmp_path: Path) -> None:
    async def scenario() -> tuple[int, int]:
        transcripts = BlockingTranscriptClient()
        runtime, delivery, redis, _ = _runtime(tmp_path, transcript_client=transcripts)
        pending = asyncio.create_task(runtime.process_message("1-0", _fields()))
        await transcripts.started.wait()
        assert redis.acked == []
        assert delivery.store is not None
        assert delivery.store.summary().pending == 0
        transcripts.release.set()
        await pending
        return len(redis.acked), delivery.store.summary().pending

    ack_count, outbox_pending = asyncio.run(scenario())
    assert ack_count == 1
    assert outbox_pending == 1


def test_lost_inbox_lease_does_not_ack_or_consume_retry_budget(tmp_path: Path) -> None:
    async def scenario() -> tuple[FakeRedis, tuple[str, int], int]:
        runtime, delivery, redis, _ = _runtime(tmp_path)
        assert runtime._inbox is not None

        def lose_lease(**_: object) -> bool:
            raise ReadyInboxLeaseLostError("ready-event processing lease was lost")

        runtime._inbox.commit_outboxed = lose_lease  # type: ignore[method-assign]
        await runtime.process_message("1-0", _fields())
        with sqlite3.connect(_settings(tmp_path).ingestion_store_path) as connection:
            row = connection.execute(
                "SELECT state, failure_count FROM meeting_transcript_ready_inbox "
                "WHERE event_key_digest = ?",
                (_event_lookup_digest(),),
            ).fetchone()
        assert row is not None
        assert delivery.store is not None
        return redis, (str(row[0]), int(row[1])), delivery.store.summary().pending

    redis, inbox_row, outbox_pending = asyncio.run(scenario())
    assert redis.acked == []
    assert redis.added == []
    assert inbox_row == ("PROCESSING", 0)
    assert outbox_pending == 0


def test_same_key_different_wire_bytes_dead_letters_metadata_only(tmp_path: Path) -> None:
    async def scenario() -> tuple[FakeRedis, int, str]:
        runtime, _, redis, transcripts = _runtime(tmp_path)
        await runtime.process_message("1-0", _fields())
        await runtime.process_message("2-0", _fields(generated_at="2026-07-18T01:02:04Z"))
        health = await runtime.health()
        return redis, transcripts.calls, health.status

    redis, calls, health_status = asyncio.run(scenario())
    assert calls == 1
    assert len(redis.acked) == 2
    assert len(redis.added) == 1
    dlq_fields = redis.added[0]["fields"]
    assert isinstance(dlq_fields, dict)
    assert dlq_fields["errorCode"] == "event_payload_hash_conflict"
    assert "payload" not in dlq_fields
    assert RAW_TRANSCRIPT not in repr(redis.added)
    assert health_status == "degraded"


def test_retryable_fetch_keeps_pel_and_consumes_one_real_failure(tmp_path: Path) -> None:
    async def scenario() -> tuple[FakeRedis, FakeTranscriptClient, int, str]:
        transcripts = FakeTranscriptClient(retry=True)
        runtime, _, redis, _ = _runtime(tmp_path, transcript_client=transcripts)
        await runtime.process_message("1-0", _fields())
        await runtime.process_message("1-0", _fields())
        with sqlite3.connect(_settings(tmp_path).ingestion_store_path) as connection:
            failure_count = int(
                connection.execute(
                    "SELECT failure_count FROM meeting_transcript_ready_inbox "
                    "WHERE event_key_digest = ?",
                    (_event_lookup_digest(),),
                ).fetchone()[0]
            )
        health = await runtime.health()
        return redis, transcripts, failure_count, health.status

    redis, transcripts, failure_count, health_status = asyncio.run(scenario())
    assert redis.acked == []
    assert redis.added == []
    assert transcripts.calls == 1
    assert failure_count == 1
    assert health_status == "degraded"


def test_invalid_event_is_durably_recorded_then_dlqed_and_acked(tmp_path: Path) -> None:
    async def scenario() -> FakeRedis:
        runtime, _, redis, _ = _runtime(tmp_path)
        await runtime.process_message(
            "9-0",
            {b"eventType": b"meeting.transcript.ready", b"payload": b"not-json"},
        )
        return redis

    redis = asyncio.run(scenario())
    assert len(redis.added) == 1
    assert len(redis.acked) == 1
    fields = redis.added[0]["fields"]
    assert isinstance(fields, dict)
    assert fields["eventKey"] == "invalid|9-0"
    assert fields["errorCode"] == "event_contract_invalid"
    assert "payload" not in fields


def test_unrelated_shared_stream_event_is_acked_without_poison_or_inbox_row(tmp_path: Path) -> None:
    async def scenario() -> tuple[FakeRedis, int, int]:
        runtime, _, redis, transcripts = _runtime(tmp_path)
        fields = _fields()
        fields[b"eventType"] = b"meeting.participant.joined"
        await runtime.process_message("7-0", fields)
        with sqlite3.connect(_settings(tmp_path).ingestion_store_path) as connection:
            row_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM meeting_transcript_ready_inbox"
                ).fetchone()[0]
            )
        return redis, transcripts.calls, row_count

    redis, transcript_calls, row_count = asyncio.run(scenario())
    assert len(redis.acked) == 1
    assert redis.added == []
    assert transcript_calls == 0
    assert row_count == 0


def test_result_outbox_full_is_backpressure_without_failure_or_ack(tmp_path: Path) -> None:
    async def scenario() -> tuple[FakeRedis, tuple[str, int], str | None, str | None]:
        runtime, delivery, redis, _ = _runtime(
            tmp_path,
            settings_overrides={"ingestion_max_rows": 1},
        )
        assert delivery.store is not None
        delivery.store.enqueue(
            analysis_run_id="existing-run",
            meeting_id=MEETING,
            payload={"summary": "already-encrypted"},
        )
        await runtime.process_message("1-0", _fields())
        with sqlite3.connect(_settings(tmp_path).ingestion_store_path) as connection:
            row = connection.execute(
                """
                    SELECT state, failure_count
                    FROM meeting_transcript_ready_inbox
                    WHERE event_key_digest = ?
                    """,
                (_event_lookup_digest(),),
            ).fetchone()
        consumer_health = await runtime.health()
        delivery_health = await delivery.health()
        assert row is not None
        return (
            redis,
            (str(row[0]), int(row[1])),
            consumer_health.error_code,
            delivery_health.error_code,
        )

    redis, row, consumer_error, delivery_error = asyncio.run(scenario())
    assert redis.acked == []
    assert redis.added == []
    assert row == ("RECEIVED", 0)
    assert consumer_error == "store_OutboxFullError"
    assert delivery_error == "OutboxFullError"


def test_owned_pel_scan_makes_retry_available_before_stale_claim_window(tmp_path: Path) -> None:
    async def scenario() -> tuple[list[object], int, int]:
        transcripts = FakeTranscriptClient(retry=True)
        runtime, _, redis, _ = _runtime(tmp_path, transcript_client=transcripts)
        await runtime.process_message("1-0", _fields())
        assert await runtime._read_owned_pending() == []
        assert redis.readgroup_calls == []
        with sqlite3.connect(_settings(tmp_path).ingestion_store_path) as connection:
            connection.execute(
                "UPDATE meeting_transcript_ready_inbox SET next_attempt_at = 0 "
                "WHERE event_key_digest = ?",
                (_event_lookup_digest(),),
            )
        runtime._owned_pending_cursor = "0-0"
        runtime._next_owned_pending_scan_monotonic = 0.0
        redis.readgroup_results = [[(b"meeting:events", [(b"1-0", _fields())])]]
        due = await runtime._read_owned_pending()
        for message_id, fields in due:
            await runtime.process_message(message_id, fields)
        return (
            [call["streams"] for call in redis.readgroup_calls],
            transcripts.calls,
            len(redis.acked),
        )

    stream_cursors, transcript_calls, ack_count = asyncio.run(scenario())
    assert stream_cursors == [{"meeting:events": "0-0"}]
    assert transcript_calls == 2
    assert ack_count == 0


def test_autoclaim_uses_returned_scan_cursor(tmp_path: Path) -> None:
    async def scenario() -> list[object]:
        runtime, _, redis, _ = _runtime(tmp_path)
        redis.autoclaim_result = (b"42-0", [], [b"deleted-1"])
        await runtime._claim_stale()
        redis.autoclaim_result = (b"0-0", [], [])
        await runtime._claim_stale()
        return redis.autoclaim_start_ids

    assert asyncio.run(scenario()) == ["0-0", "42-0"]


def test_owned_pel_finishes_current_scan_even_when_a_future_retry_is_scheduled(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[list[tuple[str, dict[object, object]]], object]:
        runtime, _, redis, _ = _runtime(tmp_path)
        runtime._owned_pending_cursor = "1-0"
        runtime._next_owned_pending_scan_monotonic = time.monotonic() + 300.0
        redis.readgroup_results = [[(b"meeting:events", [(b"2-0", _fields())])]]
        messages = await runtime._read_owned_pending()
        return messages, redis.readgroup_calls[0]["streams"]

    messages, streams = asyncio.run(scenario())
    assert messages[0][0] == "2-0"
    assert streams == {"meeting:events": "1-0"}
