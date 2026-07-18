"""Authenticated, tenant-bound canonical transcript snapshot client."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator

from app.core.config import Settings
from app.models.ready_event import ParsedTranscriptReadyEvent
from app.services.durable_outbox import ClaimedMessage
from app.services.meeting_service_client import (
    DeliveryAttempt,
    DeliveryDisposition,
    MeetingServiceTlsError,
    ReloadingHttpClient,
    ServiceTokenClient,
    ServiceTokenRequest,
    _retry_after_seconds,
)


class CanonicalTranscriptError(RuntimeError):
    """Base class whose message never contains transcript or response content."""


@dataclass
class CanonicalTranscriptRetryableError(CanonicalTranscriptError):
    error_code: str
    retry_after_sec: float | None = None


@dataclass
class CanonicalTranscriptTerminalError(CanonicalTranscriptError):
    error_code: str


class CanonicalTranscriptSegment(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", allow_inf_nan=False)

    text: str | None = Field(default=None, min_length=1, repr=False)
    start: float = Field(ge=0.0)
    end: float | None = Field(default=None, ge=0.0)

    @model_validator(mode="after")
    def _verify_timing(self) -> CanonicalTranscriptSegment:
        if self.end is not None and self.end < self.start:
            raise ValueError("canonical transcript segment end precedes start")
        return self


class CanonicalTranscriptSnapshot(BaseModel):
    """Expected backend DTO; raw text exists only for this in-memory object."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    tenant_id: str = Field(alias="tenantId")
    meeting_id: str = Field(alias="meetingId")
    session_id: str = Field(alias="sessionId")
    finalization_version: int = Field(
        alias="finalizationVersion",
        ge=1,
        le=2_147_483_647,
    )
    finalized_at: datetime = Field(alias="finalizedAt")
    state: Literal["FINALIZED"]
    transcript: str = Field(min_length=1, repr=False)
    transcript_sha256: str = Field(
        alias="transcriptSha256",
        pattern=r"^[0-9a-f]{64}$",
    )
    segment_count: int = Field(alias="segmentCount", ge=1, le=1_000_000)
    segments: list[CanonicalTranscriptSegment] = Field(min_length=1)

    @model_validator(mode="after")
    def _verify_content_hash(self) -> CanonicalTranscriptSnapshot:
        if self.finalized_at.tzinfo is None or self.finalized_at.utcoffset() is None:
            raise ValueError("canonical transcript finalizedAt must carry a timezone")
        actual = hashlib.sha256(self.transcript.encode("utf-8")).hexdigest()
        if actual != self.transcript_sha256:
            raise ValueError("canonical transcript content hash mismatch")
        if len(self.segments) != self.segment_count:
            raise ValueError("canonical transcript segment count mismatch")
        reconstructed = "\n".join(
            segment.text for segment in self.segments if segment.text is not None
        )
        if reconstructed != self.transcript:
            raise ValueError("canonical transcript does not match its segments")
        return self


@dataclass(frozen=True)
class CanonicalTranscriptFetchResult:
    snapshot: CanonicalTranscriptSnapshot


@dataclass(frozen=True)
class _CanonicalTranscriptQuery:
    tenant_id: uuid.UUID
    meeting_id: uuid.UUID
    session_id: uuid.UUID
    finalization_version: int
    analysis_run_id: uuid.UUID
    analysis_spec_version: str
    canonical_read_grant: SecretStr
    segment_count: int | None = None
    finalized_at: datetime | None = None
    transcript_sha256: str | None = None


class CanonicalTranscriptPort(Protocol):
    async def fetch(self, event: ParsedTranscriptReadyEvent) -> CanonicalTranscriptFetchResult: ...

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

    async def fetch(self, event: ParsedTranscriptReadyEvent) -> CanonicalTranscriptFetchResult:
        return await self._fetch(
            _CanonicalTranscriptQuery(
                tenant_id=event.tenant_id,
                meeting_id=event.meeting_id,
                session_id=event.session_id,
                finalization_version=event.finalization_version,
                analysis_run_id=event.analysis_run_id,
                analysis_spec_version=self._settings.analysis_spec_version,
                canonical_read_grant=event.canonical_read_grant,
                segment_count=event.segment_count,
            )
        )

    async def capability_for(
        self, message: ClaimedMessage
    ) -> tuple[SecretStr | None, DeliveryAttempt | None]:
        try:
            query = _query_from_message(message)
        except (KeyError, TypeError, ValueError):
            return None, DeliveryAttempt(
                DeliveryDisposition.TERMINAL,
                error_code="transcript_delivery_tuple_invalid",
            )
        try:
            capability, capability_expires_at = await self._issue_capability(query)
        except CanonicalTranscriptRetryableError as exc:
            return None, DeliveryAttempt(
                DeliveryDisposition.RETRY,
                error_code=exc.error_code,
                retry_after_sec=exc.retry_after_sec,
            )
        except CanonicalTranscriptTerminalError as exc:
            return None, DeliveryAttempt(
                DeliveryDisposition.TERMINAL,
                error_code=exc.error_code,
            )
        minimum_remaining = timedelta(
            seconds=(
                self._settings.ingestion_timeout_sec
                + self._settings.transcript_service_capability_clock_skew_sec
            )
        )
        if capability_expires_at <= datetime.now(UTC) + minimum_remaining:
            return None, DeliveryAttempt(
                DeliveryDisposition.RETRY,
                error_code="transcript_capability_expired",
                retry_after_sec=1.0,
            )
        return capability, None

    async def _fetch(self, query: _CanonicalTranscriptQuery) -> CanonicalTranscriptFetchResult:
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
            tenant_id=query.tenant_id,
            meeting_id=query.meeting_id,
            session_id=query.session_id,
            finalization_version=query.finalization_version,
        )
        url = self._settings.transcript_service_base_url.rstrip("/") + path
        try:
            response = await self._client.get(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-Tenant-Id": str(query.tenant_id),
                    "X-Meeting-Id": str(query.meeting_id),
                    "X-Transcript-Session-Id": str(query.session_id),
                    "X-Analysis-Run-Id": str(query.analysis_run_id),
                    "X-Analysis-Spec-Version": query.analysis_spec_version,
                    "X-Canonical-Read-Grant": query.canonical_read_grant.get_secret_value(),
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
        except (ValidationError, ValueError):
            raise CanonicalTranscriptTerminalError("transcript_invalid_response") from None
        if len(snapshot.transcript) > self._settings.max_transcript_chars:
            raise CanonicalTranscriptTerminalError("transcript_too_large")
        if (
            snapshot.tenant_id != str(query.tenant_id)
            or snapshot.meeting_id != str(query.meeting_id)
            or snapshot.session_id != str(query.session_id)
            or snapshot.finalization_version != query.finalization_version
            or (query.segment_count is not None and snapshot.segment_count != query.segment_count)
            or (query.finalized_at is not None and snapshot.finalized_at != query.finalized_at)
            or (
                query.transcript_sha256 is not None
                and snapshot.transcript_sha256 != query.transcript_sha256
            )
        ):
            raise CanonicalTranscriptTerminalError("transcript_identity_mismatch")
        return CanonicalTranscriptFetchResult(snapshot=snapshot)

    async def _issue_capability(
        self, query: _CanonicalTranscriptQuery
    ) -> tuple[SecretStr, datetime]:
        token, token_error = await self._tokens.get_token()
        if token_error is not None:
            if token_error.disposition is DeliveryDisposition.RETRY:
                raise CanonicalTranscriptRetryableError(
                    token_error.error_code,
                    token_error.retry_after_sec,
                )
            raise CanonicalTranscriptTerminalError(token_error.error_code)
        assert token is not None

        path = self._settings.transcript_service_capability_path_template.format(
            tenant_id=query.tenant_id,
            meeting_id=query.meeting_id,
            session_id=query.session_id,
            finalization_version=query.finalization_version,
        )
        url = self._settings.transcript_service_base_url.rstrip("/") + path
        try:
            response = await self._client.post(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-Tenant-Id": str(query.tenant_id),
                    "X-Meeting-Id": str(query.meeting_id),
                    "X-Transcript-Session-Id": str(query.session_id),
                    "X-Analysis-Run-Id": str(query.analysis_run_id),
                    "X-Analysis-Spec-Version": query.analysis_spec_version,
                    "X-Transcript-Finalized-At": (
                        query.finalized_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
                        if query.finalized_at is not None
                        else ""
                    ),
                    "X-Transcript-Sha256": query.transcript_sha256 or "",
                    "X-Canonical-Read-Grant": query.canonical_read_grant.get_secret_value(),
                },
                timeout=self._settings.transcript_service_timeout_sec,
            )
        except (httpx.HTTPError, MeetingServiceTlsError) as exc:
            raise CanonicalTranscriptRetryableError(
                f"transcript_capability_network_{type(exc).__name__}"
            ) from exc

        if response.status_code == 401:
            self._tokens.invalidate()
            raise CanonicalTranscriptRetryableError("transcript_capability_http_401")
        if response.status_code in {404, 408, 425, 429} or response.status_code >= 500:
            raise CanonicalTranscriptRetryableError(
                f"transcript_capability_http_{response.status_code}",
                _retry_after_seconds(response),
            )
        if response.status_code != 200:
            raise CanonicalTranscriptTerminalError(
                f"transcript_capability_http_{response.status_code}"
            )
        try:
            capability = response.headers["X-Analysis-Job-Capability"].strip()
            capability_expires_at = _aware_datetime(
                response.headers["X-Analysis-Job-Capability-Expires-At"]
            )
            if not capability:
                raise ValueError("empty capability")
        except (KeyError, ValueError):
            raise CanonicalTranscriptTerminalError(
                "transcript_capability_invalid_response"
            ) from None
        return SecretStr(capability), capability_expires_at

    async def aclose(self) -> None:
        await self._client.aclose()


def _query_from_message(message: ClaimedMessage) -> _CanonicalTranscriptQuery:
    payload = message.payload
    finalized_at = _aware_datetime(str(payload["finalized_at"]))
    transcript_sha256 = str(payload["transcript_sha256"])
    if len(transcript_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in transcript_sha256
    ):
        raise ValueError("invalid transcript hash")
    analysis_spec_version = str(payload["analysis_spec_version"])
    if not analysis_spec_version.strip() or len(analysis_spec_version) > 64:
        raise ValueError("invalid analysis spec version")
    meeting_id = uuid.UUID(message.meeting_id)
    if uuid.UUID(str(payload["meeting_id"])) != meeting_id:
        raise ValueError("meeting identity mismatch")
    finalization_version = int(str(payload["finalization_version"]))
    if finalization_version < 1:
        raise ValueError("invalid finalization version")
    return _CanonicalTranscriptQuery(
        tenant_id=uuid.UUID(str(payload["_canonical_tenant_id"])),
        meeting_id=meeting_id,
        session_id=uuid.UUID(str(payload["transcript_session_id"])),
        finalization_version=finalization_version,
        analysis_run_id=uuid.UUID(message.analysis_run_id),
        analysis_spec_version=analysis_spec_version,
        canonical_read_grant=_canonical_read_grant(payload),
        finalized_at=finalized_at,
        transcript_sha256=transcript_sha256,
    )


def _canonical_read_grant(payload: dict[str, object]) -> SecretStr:
    grant = str(payload["_canonical_read_grant"])
    if (
        len(grant) != 46
        or not grant.startswith("v1.")
        or any(
            character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
            for character in grant[3:]
        )
    ):
        raise ValueError("invalid canonical read grant")
    return SecretStr(grant)


def _aware_datetime(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must carry a timezone")
    return parsed
