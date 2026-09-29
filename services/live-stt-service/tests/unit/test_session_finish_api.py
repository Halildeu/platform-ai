"""#3746 AI-D3 — finish endpoint + Redis publisher tests.

The endpoint must answer 202 immediately, run the job on a background
thread through the single-flight gate with the store's audio, tolerate
duplicate finishes as no-ops, and reject malformed envelopes with 422.
"""

from __future__ import annotations

import json
import threading
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.session_finish import build_finish_router
from app.core.config import Settings
from app.services.attribution_publisher import RedisAttributionPublisher
from app.services.session_attribution_job import JobResult, JobStatus
from app.services.session_audio_store import SessionAudioStore

MEETING = "9b2c5a89-f39a-47da-a33d-a2859b468e8b"
SECOND_BYTES = 16_000 * 2


def finish_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "schema": "liveSttSessionFinish.v1",
        "tenantId": "42",
        "meetingId": MEETING,
        "sourceSessionId": "SES-1",
        "transportEpoch": 0,
        "expectedSampleCount": 16_000,
    }
    body.update(overrides)
    return body


class RecordingJob:
    """Stands in for run_attribution_job; signals when the thread ran it."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.ran = threading.Event()

    def __call__(self, audio, envelope, runner_config, **kwargs) -> JobResult:
        self.calls.append(
            {
                "audio": audio,
                "envelope": envelope,
                "runner_config": runner_config,
                "kwargs": kwargs,
            }
        )
        self.ran.set()
        return JobResult(JobStatus.PUBLISHED, window_count=1)


class NullPublisher:
    def publish(self, payload: dict[str, Any]) -> None:  # pragma: no cover - unused
        raise AssertionError("the fake job never publishes")


def make_client(store: SessionAudioStore, job: RecordingJob) -> TestClient:
    settings = Settings(
        session_audio_store_enabled=True,
        session_attribution_enabled=True,
        diar_hf_token="test-token",
    )
    app = FastAPI()
    app.include_router(
        build_finish_router(
            settings=settings, store=store, publisher=NullPublisher(), job=job
        )
    )
    return TestClient(app)


def test_finish_hands_store_audio_to_the_job() -> None:
    store = SessionAudioStore(cap_bytes=10 * SECOND_BYTES, idle_ttl_sec=60, max_sessions=2)
    store.append("key-1", b"\x00" * SECOND_BYTES, window_seq=0)
    job = RecordingJob()
    client = make_client(store, job)

    response = client.post("/session/key-1/finish", json=finish_body())
    assert response.status_code == 202
    assert job.ran.wait(5.0)
    [call] = job.calls
    assert call["audio"] is not None
    assert call["audio"].pcm16 == b"\x00" * SECOND_BYTES
    assert call["audio"].windows == ((0, 0, 1000),)
    assert call["envelope"].meeting_id == MEETING
    assert call["envelope"].expected_sample_count == 16_000
    # The store was consumed exactly once by the job path.
    assert store.finish("key-1") is None


def test_duplicate_finish_is_a_202_noop_with_none_audio() -> None:
    store = SessionAudioStore(cap_bytes=10 * SECOND_BYTES, idle_ttl_sec=60, max_sessions=2)
    job = RecordingJob()
    client = make_client(store, job)

    response = client.post("/session/unknown/finish", json=finish_body())
    assert response.status_code == 202
    assert job.ran.wait(5.0)
    assert job.calls[0]["audio"] is None  # job reports CANCELLED_NO_AUDIO itself


@pytest.mark.parametrize(
    "mutate",
    [
        {"schema": "liveSttSessionFinish.v2"},
        {"meetingId": "not-a-uuid"},
        {"expectedSampleCount": 10},
        {"extra": "field"},
        {"tenantId": ""},
    ],
)
def test_malformed_envelope_is_422(mutate: dict[str, object]) -> None:
    store = SessionAudioStore(cap_bytes=10 * SECOND_BYTES, idle_ttl_sec=60, max_sessions=2)
    job = RecordingJob()
    client = make_client(store, job)
    response = client.post("/session/k/finish", json=finish_body(**mutate))
    assert response.status_code == 422
    assert not job.calls


def test_runner_config_comes_from_settings() -> None:
    store = SessionAudioStore(cap_bytes=10 * SECOND_BYTES, idle_ttl_sec=60, max_sessions=2)
    store.append("k", b"\x00" * SECOND_BYTES, window_seq=0)
    job = RecordingJob()
    client = make_client(store, job)
    client.post("/session/k/finish", json=finish_body())
    assert job.ran.wait(5.0)
    config = job.calls[0]["runner_config"]
    assert config.model_name == "pyannote/speaker-diarization-3.1"
    assert config.max_speakers == 10
    kwargs = job.calls[0]["kwargs"]
    assert kwargs["dominance_threshold"] == pytest.approx(0.7)
    assert kwargs["vram_retry_attempts"] == 3


class FakeRedis:
    def __init__(self) -> None:
        self.entries: list[tuple[str, dict[str, str], int, bool]] = []

    def xadd(self, name, fields, maxlen=None, approximate=True):
        self.entries.append((name, fields, maxlen, approximate))
        return b"1-1"


def test_redis_publisher_serializes_payload_with_bounded_trim() -> None:
    redis = FakeRedis()
    publisher = RedisAttributionPublisher(redis, "stt:session:attribution")
    payload = {"schema": "directSttSessionAttribution.v1", "meetingId": MEETING, "x": 1}
    publisher.publish(payload)
    [(stream, fields, maxlen, approximate)] = redis.entries
    assert stream == "stt:session:attribution"
    assert fields["schema"] == "directSttSessionAttribution.v1"
    assert fields["meetingId"] == MEETING
    assert json.loads(fields["payload"]) == payload
    assert maxlen == 10_000
    assert approximate is True


def test_redis_publisher_bounds() -> None:
    with pytest.raises(ValueError):
        RedisAttributionPublisher(FakeRedis(), "")
    with pytest.raises(ValueError):
        RedisAttributionPublisher(FakeRedis(), "s", maxlen=10)
