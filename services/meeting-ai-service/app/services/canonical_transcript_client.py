"""Authenticated, tenant-bound canonical transcript snapshot client."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.core.config import Settings
from app.models.ready_event import ParsedTranscriptReadyEvent
from app.services.meeting_service_client import (
    DeliveryDisposition,
    MeetingServiceTlsError,
    ReloadingHttpClient,
    ServiceTokenClient,
    ServiceTokenRequest,
    _retry_after_seconds,
)


class CanonicalTranscriptError(RuntimeError):
    """Base class whose message never contains transcript or response content."""


@dataclass(frozen=True)
class CanonicalTranscriptRetryableError(CanonicalTranscriptError):
    error_code: str
    retry_after_sec: float | None = None


@dataclass(frozen=True)
class CanonicalTranscriptTerminalError(CanonicalTranscriptError):
    error_code: str


class CanonicalTranscriptSegment(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    text: str = Field(min_length=1, repr=False)
    start: float = Field(ge=0.0)
    end: float | None = Field(default=None, ge=0.0)


class CanonicalTranscriptSnapshot(BaseModel):
    """Expected backend DTO; raw text exists only for this in-memory object."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    tenant_id: str = Field(alias="tenantId")
    meeting_id: str = Field(alias="meetingId")
    session_id: str = Field(alias="sessionId")
    finalization_version: Literal[1] = Field(alias="finalizationVersion")
    state: Literal["FINALIZED"]
    transcript: str = Field(min_length=1, repr=False)
    transcript_sha256: str = Field(
        alias="transcriptSha256",
        pattern=r"^[0-9a-f]{64}$",
    )
    segment_count: int = Field(alias="segmentCount", ge=1, le=1_000_000)
    segments: list[CanonicalTranscriptSegment] | None = None

    @model_validator(mode="after")
    def _verify_content_hash(self) -> CanonicalTranscriptSnapshot:
        actual = hashlib.sha256(self.transcript.encode("utf-8")).hexdigest()
        if actual != self.transcript_sha256:
            raise ValueError("canonical transcript content hash mismatch")
        if self.segments is not None and len(self.segments) != self.segment_count:
            raise ValueError("canonical transcript segment count mismatch")
        return self


class CanonicalTranscriptPort(Protocol):
    async def fetch(self, event: ParsedTranscriptReadyEvent) -> CanonicalTranscriptSnapshot: ...

    async def aclose(self) -> None: ...


class HttpCanonicalTranscriptClient:
    """Fetch canonical text only after auth-service grants the read permission."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = ReloadingHttpClient(settings, client)
        self._tokens = ServiceTokenClient(
            settings,
            self._client,
            ServiceTokenRequest(
                token_url=settings.transcript_service_token_url,
                client_id=settings.transcript_service_client_id,
                client_secret=settings.transcript_service_client_secret,
                audience=settings.transcript_service_audience,
                permissions=tuple(settings.transcript_service_permissions),
                timeout_sec=settings.transcript_service_timeout_sec,
            ),
        )

    async def fetch(self, event: ParsedTranscriptReadyEvent) -> CanonicalTranscriptSnapshot:
        token, token_error = await self._tokens.get_token()
        if token_error is not None:
            if token_error.disposition is DeliveryDisposition.RETRY:
                raise CanonicalTranscriptRetryableError(
                    token_error.error_code,
                    token_error.retry_after_sec,
                )
            raise CanonicalTranscriptTerminalError(token_error.error_code)
        assert token is not None

        path = self._settings.transcript_service_snapshot_path_template.format(
            tenant_id=event.tenant_id,
            meeting_id=event.meeting_id,
            session_id=event.session_id,
            finalization_version=event.finalization_version,
        )
        url = self._settings.transcript_service_base_url.rstrip("/") + path
        try:
            response = await self._client.get(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-Tenant-Id": str(event.tenant_id),
                    "X-Meeting-Id": str(event.meeting_id),
                    "X-Transcript-Session-Id": str(event.session_id),
                },
                timeout=self._settings.transcript_service_timeout_sec,
            )
        except (httpx.HTTPError, MeetingServiceTlsError) as exc:
            raise CanonicalTranscriptRetryableError(
                f"transcript_network_{type(exc).__name__}"
            ) from exc

        if response.status_code == 401:
            self._tokens.invalidate()
            raise CanonicalTranscriptRetryableError("transcript_http_401")
        if response.status_code in {404, 408, 425, 429} or response.status_code >= 500:
            raise CanonicalTranscriptRetryableError(
                f"transcript_http_{response.status_code}",
                _retry_after_seconds(response),
            )
        if response.status_code != 200:
            raise CanonicalTranscriptTerminalError(f"transcript_http_{response.status_code}")
        try:
            snapshot = CanonicalTranscriptSnapshot.model_validate_json(response.content)
        except ValidationError as exc:
            raise CanonicalTranscriptTerminalError("transcript_invalid_response") from exc
        if len(snapshot.transcript) > self._settings.max_transcript_chars:
            raise CanonicalTranscriptTerminalError("transcript_too_large")
        if (
            snapshot.tenant_id != str(event.tenant_id)
            or snapshot.meeting_id != str(event.meeting_id)
            or snapshot.session_id != str(event.session_id)
            or snapshot.finalization_version != event.finalization_version
            or snapshot.segment_count != event.segment_count
        ):
            raise CanonicalTranscriptTerminalError("transcript_identity_mismatch")
        return snapshot

    async def aclose(self) -> None:
        await self._client.aclose()
