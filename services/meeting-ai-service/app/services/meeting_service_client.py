"""Authenticated transport for the meeting-service analysis-result contract."""

from __future__ import annotations

import asyncio
import email.utils
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

import httpx

from app.core.config import Settings
from app.services.durable_outbox import ClaimedMessage


class DeliveryDisposition(str, Enum):
    DELIVERED = "delivered"
    REPLAYED = "replayed"
    RETRY = "retry"
    TERMINAL = "terminal"


@dataclass(frozen=True)
class DeliveryAttempt:
    disposition: DeliveryDisposition
    error_code: str = ""
    retry_after_sec: float | None = None


@dataclass
class _CachedToken:
    access_token: str
    expires_at_monotonic: float


class ServiceTokenClient:
    """OAuth2 client-credentials token cache; credentials are never persisted."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client
        self._cached: _CachedToken | None = None
        self._lock = asyncio.Lock()

    async def get_token(self) -> tuple[str | None, DeliveryAttempt | None]:
        now = time.monotonic()
        if self._cached is not None and self._cached.expires_at_monotonic > now:
            return self._cached.access_token, None

        async with self._lock:
            now = time.monotonic()
            if self._cached is not None and self._cached.expires_at_monotonic > now:
                return self._cached.access_token, None
            try:
                response = await self._client.post(
                    self._settings.meeting_service_token_url,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self._settings.meeting_service_client_id,
                        "client_secret": (
                            self._settings.meeting_service_client_secret.get_secret_value()
                        ),
                        "scope": self._settings.meeting_service_scope,
                    },
                    timeout=self._settings.ingestion_timeout_sec,
                )
            except httpx.HTTPError as exc:
                return None, DeliveryAttempt(
                    DeliveryDisposition.RETRY,
                    error_code=f"token_network_{type(exc).__name__}",
                )

            if response.status_code == 429 or response.status_code >= 500:
                return None, DeliveryAttempt(
                    DeliveryDisposition.RETRY,
                    error_code=f"token_http_{response.status_code}",
                    retry_after_sec=_retry_after_seconds(response),
                )
            if response.status_code >= 400:
                return None, DeliveryAttempt(
                    DeliveryDisposition.TERMINAL,
                    error_code=f"token_http_{response.status_code}",
                )
            try:
                body = response.json()
                token_value = body["access_token"]
                if not isinstance(token_value, str):
                    raise TypeError("access_token must be a string")
                access_token = token_value.strip()
                expires_in = float(body.get("expires_in", 60.0))
            except (KeyError, TypeError, ValueError):
                return None, DeliveryAttempt(
                    DeliveryDisposition.TERMINAL,
                    error_code="token_invalid_response",
                )
            if not access_token or expires_in <= 0:
                return None, DeliveryAttempt(
                    DeliveryDisposition.TERMINAL,
                    error_code="token_invalid_response",
                )
            # Never extend a short-lived token to an artificial minimum TTL.
            # Tokens with <=10 s remaining are valid for this immediate attempt
            # but deliberately miss the cache on the next delivery.
            cache_ttl = max(0.0, expires_in - 10.0)
            self._cached = _CachedToken(
                access_token=access_token,
                expires_at_monotonic=now + cache_ttl,
            )
            return access_token, None

    def invalidate(self) -> None:
        self._cached = None


class MeetingServiceClient:
    """Classify delivery outcomes without logging response bodies or credentials."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client
        self._tokens = ServiceTokenClient(settings, client)

    async def deliver(self, message: ClaimedMessage) -> DeliveryAttempt:
        token, token_error = await self._tokens.get_token()
        if token_error is not None:
            return token_error
        assert token is not None
        url = (
            self._settings.meeting_service_base_url.rstrip("/")
            + f"/api/v1/internal/meetings/{message.meeting_id}/analysis-results"
        )
        try:
            response = await self._client.post(
                url,
                json=message.payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Idempotency-Key": message.analysis_run_id,
                },
                timeout=self._settings.ingestion_timeout_sec,
            )
        except httpx.HTTPError as exc:
            return DeliveryAttempt(
                DeliveryDisposition.RETRY,
                error_code=f"ingestion_network_{type(exc).__name__}",
            )

        if response.status_code in (200, 201):
            if response.status_code == 200:
                return DeliveryAttempt(DeliveryDisposition.REPLAYED)
            return DeliveryAttempt(DeliveryDisposition.DELIVERED)
        if response.status_code == 401:
            self._tokens.invalidate()
            return DeliveryAttempt(
                DeliveryDisposition.RETRY,
                error_code="ingestion_http_401",
            )
        if response.status_code == 429 or response.status_code >= 500:
            return DeliveryAttempt(
                DeliveryDisposition.RETRY,
                error_code=f"ingestion_http_{response.status_code}",
                retry_after_sec=_retry_after_seconds(response),
            )
        return DeliveryAttempt(
            DeliveryDisposition.TERMINAL,
            error_code=f"ingestion_http_{response.status_code}",
        )


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if not isinstance(parsed, datetime):
            return None
        retry_at = parsed
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        return float(max(0.0, (retry_at - datetime.now(UTC)).total_seconds()))
