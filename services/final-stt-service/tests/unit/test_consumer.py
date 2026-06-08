from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.models.schemas import FinalSttResult
from app.services.consumer import FinalSttConsumer


class FakeRedis:
    def __init__(self) -> None:
        self.added: list[tuple[str, dict[str, str]]] = []
        self.acked: list[str] = []

    def xgroup_create(self, **_kwargs: object) -> object:
        return True

    def xreadgroup(
        self, **_kwargs: object
    ) -> list[tuple[object, list[tuple[object, dict[object, object]]]]]:
        return []

    def xadd(self, name: str, fields: dict[str, str], maxlen: int, approximate: bool) -> object:
        self.added.append((name, fields))
        return "result-id"

    def xack(self, _name: str, _groupname: str, *ids: str) -> object:
        self.acked.extend(ids)
        return len(ids)


class FakeTranscriber:
    def transcribe(self, job: Any) -> FinalSttResult:
        return FinalSttResult(
            sessionId=job.session_id,
            chunkSeq=job.chunk_seq,
            correlationId=job.correlation_id,
            revisedText="nihai metin",
            finalChunkText="nihai metin",
            draftText=job.draft_text,
            overlapWords=0,
            language="tr",
            languageProbability=1.0,
            audioDurationSec=job.audio_duration_sec,
            elapsedMs=250,
            model="large-v3",
            computeType="float16",
            device="cuda",
            segments=[],
        )


def valid_fields(path: Path) -> dict[object, object]:
    return {
        b"sessionId": b"s1",
        b"chunkSeq": b"4",
        b"audioPath": str(path).encode(),
        b"audioDurationSec": b"12.0",
        b"committedText": b"",
        b"draftText": b"draft",
        b"correlationId": b"corr",
    }


def test_success_publishes_then_acks(tmp_path: Path) -> None:
    audio = tmp_path / "chunk.wav"
    audio.write_bytes(b"fake")
    redis = FakeRedis()
    settings = Settings(audio_root=tmp_path)
    consumer = FinalSttConsumer(settings, FakeTranscriber(), redis)  # type: ignore[arg-type]
    consumer.process_message("1-0", valid_fields(audio))
    assert [name for name, _fields in redis.added] == [settings.redis_result_stream] * 4
    payloads = [json.loads(fields["payload"]) for _name, fields in redis.added]
    assert [payload["state"] for payload in payloads] == [
        "draft",
        "stabilizing",
        "final",
        "revised",
    ]
    assert [payload["stateSequence"] for payload in payloads] == [0, 1, 2, 3]
    assert payloads[-1]["terminal"] is True
    assert payloads[-1]["result"]["revisedText"] == "nihai metin"
    assert redis.acked == ["1-0"]


def test_invalid_message_goes_to_dead_letter_and_acks(tmp_path: Path) -> None:
    redis = FakeRedis()
    settings = Settings(audio_root=tmp_path)
    consumer = FinalSttConsumer(settings, FakeTranscriber(), redis)  # type: ignore[arg-type]
    consumer.process_message("2-0", {b"sessionId": b"s1"})
    assert redis.added[0][0] == settings.redis_dead_letter_stream
    assert redis.acked == ["2-0"]


class FailingRedis(FakeRedis):
    def xadd(
        self,
        name: str,
        fields: dict[str, str],
        maxlen: int,
        approximate: bool,
    ) -> object:
        if len(self.added) == 2:
            raise RuntimeError("result stream unavailable")
        return super().xadd(name, fields, maxlen, approximate)


def test_publish_failure_keeps_source_pending_for_retry(tmp_path: Path) -> None:
    audio = tmp_path / "chunk.wav"
    audio.write_bytes(b"fake")
    redis = FailingRedis()
    settings = Settings(audio_root=tmp_path)
    consumer = FinalSttConsumer(settings, FakeTranscriber(), redis)  # type: ignore[arg-type]

    consumer.process_message("3-0", valid_fields(audio))

    assert len(redis.added) == 2
    assert redis.acked == []
