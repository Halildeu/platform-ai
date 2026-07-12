"""Authenticated transport for the meeting-service analysis-result contract."""

from __future__ import annotations

import asyncio
import email.utils
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

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


class MeetingServiceTlsError(RuntimeError):
    """The pinned TLS material is unavailable or cannot be loaded safely."""


_TlsFingerprint = tuple[tuple[str, int, int], ...]
_ClientFactory = Callable[[ssl.SSLContext], httpx.AsyncClient]


def _tls_paths(settings: Settings) -> tuple[Path, ...]:
    paths: list[Path] = []
    if settings.meeting_service_tls_ca_path is not None:
        paths.append(settings.meeting_service_tls_ca_path)
    if settings.meeting_service_tls_mode == "mutual":
        assert settings.meeting_service_tls_client_cert_path is not None
        assert settings.meeting_service_tls_client_key_path is not None
        paths.extend(
            (
                settings.meeting_service_tls_client_cert_path,
                settings.meeting_service_tls_client_key_path,
            )
        )
    return tuple(paths)


def _tls_fingerprint(settings: Settings) -> _TlsFingerprint:
    """Return metadata only; certificate/key contents never enter logs or state."""
    fingerprint: list[tuple[str, int, int]] = []
    try:
        for path in _tls_paths(settings):
            stat = path.stat()
            fingerprint.append((str(path), stat.st_mtime_ns, stat.st_size))
    except OSError as exc:
        raise MeetingServiceTlsError("meeting-service TLS material is unavailable") from exc
    return tuple(fingerprint)


def build_meeting_service_ssl_context(settings: Settings) -> ssl.SSLContext:
    """Build a hostname-verifying TLS context with optional client authentication."""
    try:
        context = ssl.create_default_context(
            cafile=(
                str(settings.meeting_service_tls_ca_path)
                if settings.meeting_service_tls_ca_path is not None
                else None
            )
        )
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        if settings.meeting_service_tls_mode == "mutual":
            assert settings.meeting_service_tls_client_cert_path is not None
            assert settings.meeting_service_tls_client_key_path is not None
            context.load_cert_chain(
                certfile=str(settings.meeting_service_tls_client_cert_path),
                keyfile=str(settings.meeting_service_tls_client_key_path),
            )
        return context
    except (OSError, ssl.SSLError) as exc:
        raise MeetingServiceTlsError("meeting-service TLS material could not be loaded") from exc


class ReloadingHttpClient:
    """Own an httpx client and atomically reload changed CA/client-cert material."""

    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
        *,
        client_factory: _ClientFactory | None = None,
    ) -> None:
        self._settings = settings
        self._external_client = client
        self._owned_client: httpx.AsyncClient | None = None
        self._client_factory = client_factory or (
            lambda context: httpx.AsyncClient(verify=context, trust_env=False)
        )
        self._fingerprint: _TlsFingerprint | None = None
        self._next_check = 0.0
        self._lock = asyncio.Lock()
        self._active_requests: dict[httpx.AsyncClient, int] = {}
        self._retired_clients: set[httpx.AsyncClient] = set()

    async def post(self, *args: object, **kwargs: object) -> httpx.Response:
        if self._external_client is not None:
            return await self._external_client.post(*args, **kwargs)  # type: ignore[arg-type]

        client = await self._acquire_client()
        try:
            return await client.post(*args, **kwargs)  # type: ignore[arg-type]
        finally:
            await self._release_client(client)

    async def _acquire_client(self) -> httpx.AsyncClient:
        client, close_after_unlock = await self._get_or_refresh_client(acquire=True)
        if close_after_unlock is not None:
            await close_after_unlock.aclose()
        return client

    async def _get_client(self) -> httpx.AsyncClient:
        client, close_after_unlock = await self._get_or_refresh_client(acquire=False)
        if close_after_unlock is not None:
            await close_after_unlock.aclose()
        return client

    async def _get_or_refresh_client(
        self, *, acquire: bool
    ) -> tuple[httpx.AsyncClient, httpx.AsyncClient | None]:
        if self._external_client is not None:
            return self._external_client, None

        async with self._lock:
            now = time.monotonic()
            close_after_unlock: httpx.AsyncClient | None = None
            if self._owned_client is None or now >= self._next_check:
                fingerprint = _tls_fingerprint(self._settings)
                self._next_check = now + self._settings.meeting_service_tls_reload_interval_sec
                if self._owned_client is None or fingerprint != self._fingerprint:
                    context = build_meeting_service_ssl_context(self._settings)
                    replacement = self._client_factory(context)
                    previous = self._owned_client
                    self._owned_client = replacement
                    self._fingerprint = fingerprint
                    if previous is not None:
                        if self._active_requests.get(previous, 0) > 0:
                            self._retired_clients.add(previous)
                        else:
                            close_after_unlock = previous

            assert self._owned_client is not None
            client = self._owned_client
            if acquire:
                self._active_requests[client] = self._active_requests.get(client, 0) + 1
            return client, close_after_unlock

    async def _release_client(self, client: httpx.AsyncClient) -> None:
        close_after_unlock = False
        async with self._lock:
            active = self._active_requests.get(client, 0)
            if active <= 1:
                self._active_requests.pop(client, None)
                if client in self._retired_clients:
                    self._retired_clients.remove(client)
                    close_after_unlock = True
            else:
                self._active_requests[client] = active - 1
        if close_after_unlock:
            await client.aclose()

    async def aclose(self) -> None:
        async with self._lock:
            clients = set(self._retired_clients)
            if self._owned_client is not None:
                clients.add(self._owned_client)
            self._owned_client = None
            self._fingerprint = None
            self._retired_clients.clear()
            self._active_requests.clear()
        for client in clients:
            await client.aclose()


class ServiceTokenClient:
    """OAuth2 client-credentials token cache; credentials are never persisted."""

    def __init__(self, settings: Settings, client: ReloadingHttpClient) -> None:
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
                    # `permissions` is a LIST value: httpx encodes a list into a
                    # REPEATED form field (permissions=a&permissions=b), which is how
                    # auth-service binds form.get("permissions"). A scalar would emit a
                    # single occurrence and silently drop a second permission. auth-service
                    # requires `audience` and ignores `scope` (sending scope alone yields
                    # 400 invalid_audience — the #248 live-auth bug this fixes).
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self._settings.meeting_service_client_id,
                        "client_secret": (
                            self._settings.meeting_service_client_secret.get_secret_value()
                        ),
                        "audience": self._settings.meeting_service_audience,
                        "permissions": self._settings.meeting_service_permissions,
                    },
                    timeout=self._settings.ingestion_timeout_sec,
                )
            except (httpx.HTTPError, MeetingServiceTlsError) as exc:
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

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = ReloadingHttpClient(settings, client)
        self._tokens = ServiceTokenClient(settings, self._client)

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
        except (httpx.HTTPError, MeetingServiceTlsError) as exc:
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

    async def aclose(self) -> None:
        await self._client.aclose()


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
