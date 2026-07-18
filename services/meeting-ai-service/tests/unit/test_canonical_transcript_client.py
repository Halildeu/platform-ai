"""Auth, tenant binding, and response validation for canonical transcript reads."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
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
from app.services.durable_outbox import ClaimedMessage
from app.services.meeting_service_client import DeliveryDisposition

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
        transcript_service_capability_path_template=(
            "/api/v1/internal/tenants/{tenant_id}/meetings/{meeting_id}"
            "/sessions/{session_id}/finalizations/{finalization_version}"
            "/analysis-capability"
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
                f"meeting.transcript|{SESSION}|meeting.transcript.ready|" f"{finalization_version}"
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
        "finalizedAt": "2026-07-18T01:00:00Z",
        "state": "FINALIZED",
        "transcript": transcript,
        "transcriptSha256": hashlib.sha256(transcript.encode()).hexdigest(),
        "segmentCount": 1,
        "segments": [{"text": transcript, "start": 0.0, "end": 2.0}],
    }
    body.update(overrides)
    return body


def _snapshot_response(**overrides: object) -> httpx.Response:
    return httpx.Response(200, json=_snapshot(**overrides))


def _capability_response(*, expires_at: str = "2026-07-18T01:15:00Z") -> httpx.Response:
    return httpx.Response(
        200,
        headers={
            "X-Analysis-Job-Capability": "one-use-capability",
            "X-Analysis-Job-Capability-Expires-At": expires_at,
        },
    )


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
            result = await HttpCanonicalTranscriptClient(_settings(tmp_path), client).fetch(
                _event()
            )
        assert result.snapshot.transcript == "Bütçe kararlaştırıldı."
        assert result.snapshot.finalized_at == datetime.fromisoformat("2026-07-18T01:00:00+00:00")
        assert transcript_request is not None
        return token_form, transcript_request

    form, request = asyncio.run(scenario())
    assert form["client_id"] == ["meeting-ai-ready"]
    assert form["audience"] == ["transcript-service"]
    assert form["permissions"] == ["transcript:canonical:read"]
    assert request.headers["Authorization"] == "Bearer token"
    assert request.headers["X-Tenant-Id"] == TENANT
    assert request.headers["X-Analysis-Run-Id"] == str(_event().analysis_run_id)
    assert request.headers["X-Analysis-Spec-Version"] == "meeting-intelligence-v1"
    assert request.method == "GET"
    assert str(request.url).endswith(
        f"/tenants/{TENANT}/meetings/{MEETING}/sessions/{SESSION}/finalizations/1"
    )


def test_fetch_accepts_a_later_positive_finalization_version(tmp_path: Path) -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "auth.test":
                return httpx.Response(200, json={"access_token": "token"})
            return _snapshot_response(finalizationVersion=2)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await HttpCanonicalTranscriptClient(_settings(tmp_path), client).fetch(
                _event(finalization_version=2)
            )
        assert result.snapshot.finalization_version == 2

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "override",
    [
        {"tenantId": "44444444-4444-4444-8444-444444444444"},
        {"state": "DRAFT"},
        {"finalizationVersion": 2},
        {"transcriptSha256": "0" * 64},
        {"segmentCount": 2},
        {"finalizedAt": "2026-07-18T01:00:00"},
        {"segments": []},
        {"segments": [{"text": "Bütçe kararlaştırıldı.", "start": 2.0, "end": 1.0}]},
        {"segments": [{"text": "Bütçe kararlaştırıldı.", "start": float("nan"), "end": 2.0}]},
        {"segments": [{"text": "Başka metin", "start": 0.0, "end": 2.0}]},
    ],
)
def test_foreign_nonfinalized_or_hash_mismatch_is_terminal(
    tmp_path: Path, override: dict[str, object]
) -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "auth.test":
                return httpx.Response(200, json={"access_token": "token"})
            return _snapshot_response(**override)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(CanonicalTranscriptTerminalError):
                await HttpCanonicalTranscriptClient(_settings(tmp_path), client).fetch(_event())

    asyncio.run(scenario())


def test_invalid_snapshot_does_not_retain_raw_transcript_in_exception_chain(
    tmp_path: Path,
) -> None:
    raw = "RAW-TRANSCRIPT-MUST-NOT-ESCAPE"

    async def scenario() -> CanonicalTranscriptTerminalError:
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "auth.test":
                return httpx.Response(200, json={"access_token": "token"})
            body = _snapshot(
                transcript=raw,
                transcriptSha256=hashlib.sha256(raw.encode()).hexdigest(),
                segments=[{"text": "different", "start": 0.0, "end": 1.0}],
            )
            return httpx.Response(200, json=body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(CanonicalTranscriptTerminalError) as captured:
                await HttpCanonicalTranscriptClient(_settings(tmp_path), client).fetch(_event())
        return captured.value

    error = asyncio.run(scenario())
    assert error.__cause__ is None
    assert raw not in repr(error)


def test_missing_segments_is_terminal(tmp_path: Path) -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "auth.test":
                return httpx.Response(200, json={"access_token": "token"})
            body = _snapshot()
            body.pop("segments")
            return httpx.Response(200, json=body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(CanonicalTranscriptTerminalError) as captured:
                await HttpCanonicalTranscriptClient(_settings(tmp_path), client).fetch(_event())
        assert captured.value.error_code == "transcript_invalid_response"

    asyncio.run(scenario())


def test_delivery_capability_request_carries_exact_outboxed_tuple(
    tmp_path: Path,
) -> None:
    async def scenario():  # type: ignore[no-untyped-def]
        request_headers: httpx.Headers | None = None

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal request_headers
            if request.url.host == "auth.test":
                return httpx.Response(200, json={"access_token": "token"})
            request_headers = request.headers
            assert request.method == "POST"
            return httpx.Response(409)

        message = ClaimedMessage(
            analysis_run_id="44444444-4444-4444-8444-444444444444",
            meeting_id=MEETING,
            payload={
                "_canonical_tenant_id": TENANT,
                "meeting_id": MEETING,
                "transcript_session_id": SESSION,
                "transcript_sha256": _snapshot()["transcriptSha256"],
                "finalization_version": 1,
                "finalized_at": "2026-07-18T01:00:00Z",
                "analysis_spec_version": "meeting-intelligence-v1",
            },
            attempt_count=2,
            created_at=1.0,
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            capability, error = await HttpCanonicalTranscriptClient(
                _settings(tmp_path), client
            ).capability_for(message)
        return capability, error, request_headers

    capability, error, headers = asyncio.run(scenario())
    assert capability is None
    assert error is not None
    assert error.disposition is DeliveryDisposition.TERMINAL
    assert error.error_code == "transcript_capability_http_409"
    assert headers is not None
    assert headers["X-Analysis-Run-Id"] == "44444444-4444-4444-8444-444444444444"
    assert headers["X-Analysis-Spec-Version"] == "meeting-intelligence-v1"
    assert headers["X-Transcript-Finalized-At"] == "2026-07-18T01:00:00Z"
    assert headers["X-Transcript-Sha256"] == _snapshot()["transcriptSha256"]


def test_delivery_refresh_retries_when_capability_has_no_delivery_window(
    tmp_path: Path,
) -> None:
    async def scenario():  # type: ignore[no-untyped-def]
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "auth.test":
                return httpx.Response(200, json={"access_token": "token"})
            assert request.method == "POST"
            return _capability_response(expires_at="2026-07-18T01:00:00Z")

        message = ClaimedMessage(
            analysis_run_id="44444444-4444-4444-8444-444444444444",
            meeting_id=MEETING,
            payload={
                "_canonical_tenant_id": TENANT,
                "meeting_id": MEETING,
                "transcript_session_id": SESSION,
                "transcript_sha256": _snapshot()["transcriptSha256"],
                "finalization_version": 1,
                "finalized_at": "2026-07-18T01:00:00Z",
                "analysis_spec_version": "meeting-intelligence-v1",
            },
            attempt_count=2,
            created_at=1.0,
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await HttpCanonicalTranscriptClient(_settings(tmp_path), client).capability_for(
                message
            )

    capability, error = asyncio.run(scenario())
    assert capability is None
    assert error is not None
    assert error.disposition is DeliveryDisposition.RETRY
    assert error.error_code == "transcript_capability_expired"
    assert error.retry_after_sec == 1.0


def test_delivery_window_covers_backend_timeout_and_clock_skew(tmp_path: Path) -> None:
    async def scenario():  # type: ignore[no-untyped-def]
        expires_at = (datetime.now(UTC) + timedelta(seconds=14)).isoformat()

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "auth.test":
                return httpx.Response(200, json={"access_token": "token"})
            return _capability_response(expires_at=expires_at)

        message = ClaimedMessage(
            analysis_run_id="44444444-4444-4444-8444-444444444444",
            meeting_id=MEETING,
            payload={
                "_canonical_tenant_id": TENANT,
                "meeting_id": MEETING,
                "transcript_session_id": SESSION,
                "transcript_sha256": _snapshot()["transcriptSha256"],
                "finalization_version": 1,
                "finalized_at": "2026-07-18T01:00:00Z",
                "analysis_spec_version": "meeting-intelligence-v1",
            },
            attempt_count=1,
            created_at=1.0,
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await HttpCanonicalTranscriptClient(_settings(tmp_path), client).capability_for(
                message
            )

    capability, error = asyncio.run(scenario())
    assert capability is None
    assert error is not None
    assert error.disposition is DeliveryDisposition.RETRY
    assert error.error_code == "transcript_capability_expired"


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
