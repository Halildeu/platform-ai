from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

import pytest
from redis import Redis

from app.core.config import Settings
from app.models.schemas import FinalSttResult
from app.services.consumer import FinalSttConsumer

pytestmark = pytest.mark.integration


class FakeTranscriber:
    def transcribe(self, job: Any) -> FinalSttResult:
        return FinalSttResult(
            sessionId=job.session_id,
            chunkSeq=job.chunk_seq,
            correlationId=job.correlation_id,
            revisedText="gercek redis sonucu",
            finalChunkText="gercek redis sonucu",
            draftText=job.draft_text,
            overlapWords=0,
            language="tr",
            languageProbability=1.0,
            audioDurationSec=job.audio_duration_sec,
            elapsedMs=25,
            model="large-v3",
            computeType="float16",
            device="cuda",
            segments=[],
        )


def _settings(tmp_path: Path) -> Settings:
    namespace = uuid.uuid4().hex
    return Settings(
        audio_root=tmp_path,
        redis_enabled=True,
        redis_url=os.environ["FINAL_STT_TEST_REDIS_URL"],
        redis_input_stream=f"test:{namespace}:jobs",
        redis_result_stream=f"test:{namespace}:results",
        redis_dead_letter_stream=f"test:{namespace}:dead",
        redis_consumer_group=f"test-{namespace}",
        redis_consumer_name="pytest",
    )


def test_real_redis_publish_consume_result_and_ack(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    redis = Redis.from_url(settings.redis_url, decode_responses=False)
    audio = tmp_path / "chunk.wav"
    audio.write_bytes(b"fake-audio-reference")
    consumer = FinalSttConsumer(settings, FakeTranscriber(), redis)  # type: ignore[arg-type]

    try:
        consumer.ensure_group()
        message_id = redis.xadd(
            settings.redis_input_stream,
            {
                "sessionId": "session",
                "chunkSeq": "7",
                "audioPath": str(audio),
                "audioDurationSec": "12.0",
                "committedText": "",
                "draftText": "taslak",
                "correlationId": "corr-real-redis",
            },
        )
        records = redis.xreadgroup(
            groupname=settings.redis_consumer_group,
            consumername=settings.redis_consumer_name,
            streams={settings.redis_input_stream: ">"},
            count=1,
            block=1000,
        )
        assert len(records) == 1
        _stream, messages = records[0]
        assert len(messages) == 1
        received_id, fields = messages[0]
        assert received_id == message_id

        consumer.process_message(received_id.decode(), fields)

        result_records = redis.xrange(settings.redis_result_stream)
        assert len(result_records) == 1
        payload = json.loads(result_records[0][1][b"payload"])
        assert payload["revisedText"] == "gercek redis sonucu"
        assert payload["chunkSeq"] == 7
        assert (
            redis.xpending(settings.redis_input_stream, settings.redis_consumer_group)["pending"]
            == 0
        )
    finally:
        redis.delete(
            settings.redis_input_stream,
            settings.redis_result_stream,
            settings.redis_dead_letter_stream,
        )


def test_real_redis_invalid_job_dead_letters_and_acks(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    redis = Redis.from_url(settings.redis_url, decode_responses=False)
    consumer = FinalSttConsumer(settings, FakeTranscriber(), redis)  # type: ignore[arg-type]

    try:
        consumer.ensure_group()
        message_id = redis.xadd(settings.redis_input_stream, {"sessionId": "missing-fields"})
        records = redis.xreadgroup(
            groupname=settings.redis_consumer_group,
            consumername=settings.redis_consumer_name,
            streams={settings.redis_input_stream: ">"},
            count=1,
            block=1000,
        )
        _stream, messages = records[0]
        received_id, fields = messages[0]
        assert received_id == message_id

        consumer.process_message(received_id.decode(), fields)

        dead_records = redis.xrange(settings.redis_dead_letter_stream)
        assert len(dead_records) == 1
        assert dead_records[0][1][b"errorClass"] == b"ValidationError"
        assert (
            redis.xpending(settings.redis_input_stream, settings.redis_consumer_group)["pending"]
            == 0
        )
    finally:
        redis.delete(
            settings.redis_input_stream,
            settings.redis_result_stream,
            settings.redis_dead_letter_stream,
        )
