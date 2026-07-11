"""HTTP and token classification tests for canonical meeting-service delivery."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import httpx
from pydantic import SecretStr

from app.core.config import Settings
from app.services.durable_outbox import ClaimedMessage
from app.services.meeting_service_client import DeliveryDisposition, MeetingServiceClient

KEY = base64.b64encode(b"K" * 32).decode()


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        ingestion_enabled=True,
        meeting_service_base_url="https://meeting.test",
        meeting_service_token_url="https://auth.test/token",
        meeting_service_client_id="meeting-ai",
        meeting_service_client_secret=SecretStr("secret"),
        ingestion_store_path=tmp_path / "outbox.sqlite3",
        ingestion_active_key_id="v1",
        ingestion_encryption_keys_json=SecretStr(json.dumps({"v1": KEY})),
        ingestion_timeout_sec=1.0,
        ingestion_lease_sec=3.0,
    )


def _message() -> ClaimedMessage:
    return ClaimedMessage(
        analysis_run_id="22222222-2222-4222-8222-222222222222",
        meeting_id="11111111-1111-4111-8111-111111111111",
        payload={"summary": "A"},
        attempt_count=1,
        created_at=1.0,
    )


def test_token_is_cached_and_idempotency_header_is_stable(tmp_path: Path) -> None:
    async def scenario() -> None:
        token_calls = 0
        ingestion_headers: list[httpx.Headers] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal token_calls
            if request.url.host == "auth.test":
                token_calls += 1
                return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
            ingestion_headers.append(request.headers)
            return httpx.Response(201, json={"idempotent_replay": False})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            service = MeetingServiceClient(_settings(tmp_path), client)
            assert (await service.deliver(_message())).disposition is DeliveryDisposition.DELIVERED
            assert (await service.deliver(_message())).disposition is DeliveryDisposition.DELIVERED
        assert token_calls == 1
        assert all(h["Idempotency-Key"] == _message().analysis_run_id for h in ingestion_headers)
        assert all(h["Authorization"] == "Bearer token" for h in ingestion_headers)

    asyncio.run(scenario())


def test_401_invalidates_token_and_is_retryable(tmp_path: Path) -> None:
    async def scenario() -> None:
        token_calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal token_calls
            if request.url.host == "auth.test":
                token_calls += 1
                return httpx.Response(200, json={"access_token": f"token-{token_calls}"})
            return httpx.Response(401)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            service = MeetingServiceClient(_settings(tmp_path), client)
            first = await service.deliver(_message())
            second = await service.deliver(_message())
        assert first.disposition is DeliveryDisposition.RETRY
        assert second.disposition is DeliveryDisposition.RETRY
        assert token_calls == 2

    asyncio.run(scenario())


def test_short_lived_token_is_not_reused_and_invalid_token_is_terminal(tmp_path: Path) -> None:
    async def short_lived_scenario() -> int:
        token_calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal token_calls
            if request.url.host == "auth.test":
                token_calls += 1
                return httpx.Response(
                    200,
                    json={"access_token": f"token-{token_calls}", "expires_in": 5},
                )
            return httpx.Response(201)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            service = MeetingServiceClient(_settings(tmp_path), client)
            await service.deliver(_message())
            await service.deliver(_message())
        return token_calls

    async def invalid_scenario() -> DeliveryDisposition:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"access_token": None, "expires_in": 60})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await MeetingServiceClient(_settings(tmp_path), client).deliver(_message())
        return result.disposition

    assert asyncio.run(short_lived_scenario()) == 2
    assert asyncio.run(invalid_scenario()) is DeliveryDisposition.TERMINAL


def test_429_honors_retry_after_and_400_is_terminal(tmp_path: Path) -> None:
    async def scenario(status: int, headers: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "auth.test":
                return httpx.Response(200, json={"access_token": "token"})
            return httpx.Response(status, headers=headers)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await MeetingServiceClient(_settings(tmp_path), client).deliver(_message())

    throttled = asyncio.run(scenario(429, {"Retry-After": "17"}))
    rejected = asyncio.run(scenario(400))
    assert throttled.disposition is DeliveryDisposition.RETRY
    assert throttled.retry_after_sec == 17.0
    assert rejected.disposition is DeliveryDisposition.TERMINAL
