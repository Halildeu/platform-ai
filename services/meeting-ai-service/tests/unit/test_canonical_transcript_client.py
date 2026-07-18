"""Auth, tenant binding, and response validation for canonical transcript reads."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.models.ready_event import parse_transcript_ready_event
from app.services.canonical_transcript_client import (
    CanonicalTranscriptRetryableError,
    CanonicalTranscriptTerminalError,
    HttpCanonicalTranscriptClient,
)

MEETING = "11111111-1111-4111-8111-111111111111"
SESSION = "22222222-2222-4222-8222-222222222222"
TENANT = "33333333-3333-4333-8333-333333333333"


def _settings(tmp_path: Path) -> Settings:
    key = base64.b64encode(b"K" * 32).decode()
    return Settings(
        ingestion_enabled=True,
        meeting_service_base_url="https://meeting.test",
        meeting_service_token_url="https://auth.test/token",
        meeting_service_client_id="meeting-ai",
        meeting_service_client_secret=SecretStr("meeting-secret"),
        ingestion_store_path=tmp_path / "delivery.sqlite3",
        ingestion_active_key_id="v1",
        ingestion_encryption_keys_json=SecretStr(json.dumps({"v1": key})),
        ready_consumer_enabled=True,
        ready_producer_replay_horizon_sec=604_800.0,
        ready_redis_url=SecretStr("redis://redis.test:6379/0"),
        transcript_service_base_url="https://transcript.test",
        transcript_service_snapshot_path_template=(
            "/api/v1/internal/tenants/{tenant_id}/meetings/{meeting_id}"
            "/sessions/{session_id}/finalizations/{finalization_version}"
        ),
        transcript_service_token_url="https://auth.test/token",
        transcript_service_client_id="meeting-ai-ready",
        transcript_service_client_secret=SecretStr("transcript-secret"),
    )


def _event(*, finalization_version: int = 1):  # type: ignore[no-untyped-def]
    payload = json.dumps(
        {
            "schema": "meeting.event.v1",
            "eventType": "meeting.transcript.ready",
            "analysisRunId": None,
            "meetingId": MEETING,
            "tenantId": TENANT,
            "orgId": TENANT,
            "generatedAt": "2026-07-18T01:02:03Z",
            "transcriptSessionId": SESSION,
            "finalizationVersion": finalization_version,
            "segmentCount": 1,
        },
        separators=(",", ":"),
    )
    return parse_transcript_ready_event(
        {
            "eventKey": (
                f"meeting.transcript|{SESSION}|meeting.transcript.ready|"
                f"{finalization_version}"
            ),
            "eventType": "meeting.transcript.ready",
            "aggregateId": SESSION,
            "meetingId": MEETING,
            "tenantId": TENANT,
            "orgId": TENANT,
            "payload": payload,
        },
        analysis_spec_version="meeting-intelligence-v1",
    )


def _snapshot(**overrides: object) -> dict[str, object]:
    transcript = "Bütçe kararlaştırıldı."
    body: dict[str, object] = {
        "tenantId": TENANT,
        "meetingId": MEETING,
        "sessionId": SESSION,
        "finalizationVersion": 1,
        "state": "FINALIZED",
        "transcript": transcript,
        "transcriptSha256": hashlib.sha256(transcript.encode()).hexdigest(),
        "segmentCount": 1,
        "segments": [{"text": transcript, "start": 0.0, "end": 2.0}],
    }
    body.update(overrides)
    return body


def test_fetch_uses_separate_least_privilege_token_and_tenant_bound_path(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[dict[str, list[str]], httpx.Request]:
        token_form: dict[str, list[str]] = {}
        transcript_request: httpx.Request | None = None

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal transcript_request
            if request.url.host == "auth.test":
                token_form.update(parse_qs(request.content.decode()))
                return httpx.Response(200, json={"access_token": "token", "expires_in": 60})
            transcript_request = request
            return httpx.Response(200, json=_snapshot())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            snapshot = await HttpCanonicalTranscriptClient(_settings(tmp_path), client).fetch(
                _event()
            )
        assert snapshot.transcript == "Bütçe kararlaştırıldı."
        assert transcript_request is not None
        return token_form, transcript_request

    form, request = asyncio.run(scenario())
    assert form["client_id"] == ["meeting-ai-ready"]
    assert form["audience"] == ["transcript-service"]
    assert form["permissions"] == ["transcript:canonical:read"]
    assert request.headers["Authorization"] == "Bearer token"
    assert request.headers["X-Tenant-Id"] == TENANT
    assert str(request.url).endswith(
        f"/tenants/{TENANT}/meetings/{MEETING}/sessions/{SESSION}/finalizations/1"
    )


def test_fetch_accepts_a_later_positive_finalization_version(tmp_path: Path) -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "auth.test":
                return httpx.Response(200, json={"access_token": "token"})
            return httpx.Response(200, json=_snapshot(finalizationVersion=2))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            snapshot = await HttpCanonicalTranscriptClient(_settings(tmp_path), client).fetch(
                _event(finalization_version=2)
            )
        assert snapshot.finalization_version == 2

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "override",
    [
        {"tenantId": "44444444-4444-4444-8444-444444444444"},
        {"state": "DRAFT"},
        {"finalizationVersion": 2},
        {"transcriptSha256": "0" * 64},
        {"segmentCount": 2},
    ],
)
def test_foreign_nonfinalized_or_hash_mismatch_is_terminal(
    tmp_path: Path, override: dict[str, object]
) -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "auth.test":
                return httpx.Response(200, json={"access_token": "token"})
            return httpx.Response(200, json=_snapshot(**override))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(CanonicalTranscriptTerminalError):
                await HttpCanonicalTranscriptClient(_settings(tmp_path), client).fetch(_event())

    asyncio.run(scenario())


def test_temporarily_missing_snapshot_is_retryable_without_response_body(
    tmp_path: Path,
) -> None:
    async def scenario() -> CanonicalTranscriptRetryableError:
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "auth.test":
                return httpx.Response(200, json={"access_token": "token"})
            return httpx.Response(404, text="RAW-TRANSCRIPT-MUST-NOT-ESCAPE")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(CanonicalTranscriptRetryableError) as captured:
                await HttpCanonicalTranscriptClient(_settings(tmp_path), client).fetch(_event())
        return captured.value

    error = asyncio.run(scenario())
    assert error.error_code == "transcript_http_404"
    assert "RAW-TRANSCRIPT" not in repr(error)
