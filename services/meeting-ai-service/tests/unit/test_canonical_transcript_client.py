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
RUN_ID = "44444444-4444-4444-8444-444444444444"


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
        transcript_service_client_id="meeting-ai",
        transcript_service_client_secret=SecretStr("transcript-secret"),
    )


def _event(*, finalization_version: int = 1):  # type: ignore[no-untyped-def]
    payload = json.dumps(
        {
            "schema": "meeting.event.v1",
            "eventType": "meeting.transcript.ready",
            "analysisRunId": RUN_ID,
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
        204,
        headers={
            "X-Analysis-Job-Capability": "one-use-capability",
            "X-Analysis-Job-Capability-Expires-At": expires_at,
        },
    )


def _message() -> ClaimedMessage:
    return ClaimedMessage(
        analysis_run_id=RUN_ID,
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
            return httpx.Response(
                200,
                json=_snapshot(),
                headers={
                    "X-Analysis-Job-Capability": "must-not-be-parsed-from-get",
                    "X-Analysis-Job-Capability-Expires-At": "not-a-timestamp",
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await HttpCanonicalTranscriptClient(_settings(tmp_path), client).fetch(
                _event()
            )
        assert result.transcript == "Bütçe kararlaştırıldı."
        assert result.finalized_at == datetime.fromisoformat("2026-07-18T01:00:00+00:00")
        assert transcript_request is not None
        assert not hasattr(result, "capability")
        return token_form, transcript_request

    form, request = asyncio.run(scenario())
    assert form["client_id"] == ["meeting-ai"]
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
        assert result.finalization_version == 2

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
            return httpx.Response(
                200,
                json=body,
                headers={
                    "X-Analysis-Job-Capability": "one-use-capability",
                    "X-Analysis-Job-Capability-Expires-At": "2099-12-31T23:59:00Z",
                },
            )

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
            return httpx.Response(
                200,
                json=body,
                headers={
                    "X-Analysis-Job-Capability": "one-use-capability",
                    "X-Analysis-Job-Capability-Expires-At": "2099-12-31T23:59:00Z",
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(CanonicalTranscriptTerminalError) as captured:
                await HttpCanonicalTranscriptClient(_settings(tmp_path), client).fetch(_event())
        assert captured.value.error_code == "transcript_invalid_response"

    asyncio.run(scenario())


def test_snapshot_and_capability_use_separate_permissions_without_second_transfer(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[
        list[dict[str, list[str]]],
        list[httpx.Request],
        str,
    ]:
        token_forms: list[dict[str, list[str]]] = []
        service_requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "auth.test":
                form = parse_qs(request.content.decode())
                token_forms.append(form)
                permission = form["permissions"][0]
                return httpx.Response(
                    200,
                    json={"access_token": permission, "expires_in": 60},
                )
            service_requests.append(request)
            if request.method == "GET":
                return _snapshot_response()
            return _capability_response(expires_at="2099-12-31T23:59:00Z")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = HttpCanonicalTranscriptClient(_settings(tmp_path), client)
            snapshot = await adapter.fetch(_event())
            capability, error = await adapter.capability_for(_message())
        assert snapshot.transcript == "Bütçe kararlaştırıldı."
        assert capability is not None
        assert error is None
        return token_forms, service_requests, capability.get_secret_value()

    forms, requests, capability = asyncio.run(scenario())
    assert capability == "one-use-capability"
    assert [form["permissions"] for form in forms] == [
        ["transcript:canonical:read"],
        ["transcript:analysis-job-capability:issue"],
    ]
    assert all(form["client_id"] == ["meeting-ai"] for form in forms)
    assert all(form["audience"] == ["transcript-service"] for form in forms)
    assert [request.method for request in requests] == ["GET", "POST"]
    assert sum(request.method == "GET" for request in requests) == 1

    capability_request = requests[1]
    assert str(capability_request.url).endswith(
        f"/tenants/{TENANT}/meetings/{MEETING}/sessions/{SESSION}"
        "/finalizations/1/analysis-capability"
    )
    custom_headers = {
        name.lower(): value
        for name, value in capability_request.headers.items()
        if name.lower().startswith("x-")
    }
    assert custom_headers == {
        "x-tenant-id": TENANT,
        "x-meeting-id": MEETING,
        "x-transcript-session-id": SESSION,
        "x-transcript-finalization-version": "1",
        "x-analysis-run-id": RUN_ID,
        "x-analysis-spec-version": "meeting-intelligence-v1",
    }
    assert capability_request.content == b""
    assert "Bütçe kararlaştırıldı." not in repr(capability_request)


def test_delivery_refresh_retries_when_capability_has_no_delivery_window(
    tmp_path: Path,
) -> None:
    async def scenario():  # type: ignore[no-untyped-def]
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "auth.test":
                return httpx.Response(200, json={"access_token": "token"})
            assert request.method == "POST"
            return _capability_response(expires_at="2026-07-18T01:00:00Z")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await HttpCanonicalTranscriptClient(_settings(tmp_path), client).capability_for(
                _message()
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

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await HttpCanonicalTranscriptClient(_settings(tmp_path), client).capability_for(
                _message()
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


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {
            "X-Analysis-Job-Capability": "",
            "X-Analysis-Job-Capability-Expires-At": "2099-12-31T23:59:00Z",
        },
        {
            "X-Analysis-Job-Capability": "x" * 8193,
            "X-Analysis-Job-Capability-Expires-At": "2099-12-31T23:59:00Z",
        },
        {
            "X-Analysis-Job-Capability": "one-use-capability",
            "X-Analysis-Job-Capability-Expires-At": "x" * 129,
        },
        {
            "X-Analysis-Job-Capability": "one-use-capability",
            "X-Analysis-Job-Capability-Expires-At": "not-a-timestamp",
        },
    ],
)
def test_capability_response_headers_are_required_and_bounded(
    tmp_path: Path,
    headers: dict[str, str],
) -> None:
    async def scenario():  # type: ignore[no-untyped-def]
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "auth.test":
                return httpx.Response(200, json={"access_token": "token"})
            return httpx.Response(204, headers=headers)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await HttpCanonicalTranscriptClient(_settings(tmp_path), client).capability_for(
                _message()
            )

    capability, error = asyncio.run(scenario())
    assert capability is None
    assert error is not None
    assert error.disposition is DeliveryDisposition.TERMINAL
    assert error.error_code == "transcript_capability_invalid_response"


@pytest.mark.parametrize("status_code", [200, 201, 202, 205])
def test_capability_accepts_exactly_status_204(tmp_path: Path, status_code: int) -> None:
    async def scenario():  # type: ignore[no-untyped-def]
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "auth.test":
                return httpx.Response(200, json={"access_token": "token"})
            return httpx.Response(
                status_code,
                headers={
                    "X-Analysis-Job-Capability": "one-use-capability",
                    "X-Analysis-Job-Capability-Expires-At": "2099-12-31T23:59:00Z",
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await HttpCanonicalTranscriptClient(_settings(tmp_path), client).capability_for(
                _message()
            )

    capability, error = asyncio.run(scenario())
    assert capability is None
    assert error is not None
    assert error.disposition is DeliveryDisposition.TERMINAL
    assert error.error_code == f"transcript_capability_http_{status_code}"


def test_snapshot_and_capability_token_errors_have_separate_codes(tmp_path: Path) -> None:
    async def scenario() -> tuple[str, str]:
        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.host == "auth.test"
            return httpx.Response(503)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = HttpCanonicalTranscriptClient(_settings(tmp_path), client)
            with pytest.raises(CanonicalTranscriptRetryableError) as captured:
                await adapter.fetch(_event())
            _, capability_error = await adapter.capability_for(_message())
        assert capability_error is not None
        return captured.value.error_code, capability_error.error_code

    snapshot_code, capability_code = asyncio.run(scenario())
    assert snapshot_code == "transcript_token_http_503"
    assert capability_code == "transcript_capability_token_http_503"


def test_capability_401_invalidates_only_capability_token_cache(tmp_path: Path) -> None:
    async def scenario() -> tuple[list[str], list[str]]:
        token_requests: list[str] = []
        service_authorizations: list[str] = []
        capability_posts = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal capability_posts
            if request.url.host == "auth.test":
                permission = parse_qs(request.content.decode())["permissions"][0]
                token_requests.append(permission)
                token_number = token_requests.count(permission)
                return httpx.Response(
                    200,
                    json={
                        "access_token": f"{permission}-{token_number}",
                        "expires_in": 60,
                    },
                )
            service_authorizations.append(request.headers["Authorization"])
            if request.method == "GET":
                return _snapshot_response()
            capability_posts += 1
            if capability_posts == 1:
                return httpx.Response(401)
            return _capability_response(expires_at="2099-12-31T23:59:00Z")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = HttpCanonicalTranscriptClient(_settings(tmp_path), client)
            await adapter.fetch(_event())
            _, first_error = await adapter.capability_for(_message())
            capability, second_error = await adapter.capability_for(_message())
            await adapter.fetch(_event())
        assert first_error is not None
        assert first_error.error_code == "transcript_capability_http_401"
        assert capability is not None
        assert second_error is None
        return token_requests, service_authorizations

    token_requests, authorizations = asyncio.run(scenario())
    assert token_requests == [
        "transcript:canonical:read",
        "transcript:analysis-job-capability:issue",
        "transcript:analysis-job-capability:issue",
    ]
    assert authorizations == [
        "Bearer transcript:canonical:read-1",
        "Bearer transcript:analysis-job-capability:issue-1",
        "Bearer transcript:analysis-job-capability:issue-2",
        "Bearer transcript:canonical:read-1",
    ]


class _OversizedStream(httpx.AsyncByteStream):
    def __init__(self, *, chunk_size: int, chunk_count: int) -> None:
        self.chunk_size = chunk_size
        self.chunk_count = chunk_count
        self.chunks_yielded = 0

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        for _ in range(self.chunk_count):
            self.chunks_yielded += 1
            yield b"x" * self.chunk_size


def test_snapshot_stream_without_content_length_stops_at_response_byte_limit(
    tmp_path: Path,
) -> None:
    stream = _OversizedStream(chunk_size=700, chunk_count=10)

    async def scenario() -> CanonicalTranscriptTerminalError:
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "auth.test":
                return httpx.Response(200, json={"access_token": "token"})
            return httpx.Response(200, stream=stream)

        settings = _settings(tmp_path)
        object.__setattr__(settings, "transcript_service_max_response_bytes", 2_000)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(CanonicalTranscriptTerminalError) as captured:
                await HttpCanonicalTranscriptClient(settings, client).fetch(_event())
        return captured.value

    error = asyncio.run(scenario())
    assert error.error_code == "transcript_response_too_large"
    assert stream.chunks_yielded == 3


@pytest.mark.parametrize("content_length", ["invalid", "-1"])
def test_invalid_content_length_is_terminal(tmp_path: Path, content_length: str) -> None:
    async def scenario() -> CanonicalTranscriptTerminalError:
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "auth.test":
                return httpx.Response(200, json={"access_token": "token"})
            return httpx.Response(
                200,
                headers={"Content-Length": content_length},
                stream=_OversizedStream(chunk_size=1, chunk_count=1),
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(CanonicalTranscriptTerminalError) as captured:
                await HttpCanonicalTranscriptClient(_settings(tmp_path), client).fetch(_event())
        return captured.value

    error = asyncio.run(scenario())
    assert error.error_code == "transcript_invalid_content_length"
