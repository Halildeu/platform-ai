"""Pinned DTOs for the ``meeting.transcript.ready`` v1 wire contract."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

_ANALYSIS_RUN_NAMESPACE = uuid.UUID("c168bba6-cdf1-5a38-9162-7de65eb0325c")


class ReadyEventContractError(ValueError):
    """The Redis record is not the pinned, content-free ready-event contract."""


class TranscriptReadyEnvelope(BaseModel):
    """Frozen ``meeting.event.v1`` transcript-ready payload JSON."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    schema_name: Literal["meeting.event.v1"] = Field(alias="schema")
    event_type: Literal["meeting.transcript.ready"] = Field(alias="eventType")
    analysis_run_id: None = Field(alias="analysisRunId")
    meeting_id: uuid.UUID = Field(alias="meetingId")
    tenant_id: uuid.UUID = Field(alias="tenantId")
    org_id: uuid.UUID = Field(alias="orgId")
    generated_at: datetime = Field(alias="generatedAt")
    transcript_session_id: uuid.UUID = Field(alias="transcriptSessionId")
    finalization_version: Annotated[int, Field(ge=1, le=2_147_483_647)] = Field(
        alias="finalizationVersion"
    )
    segment_count: Annotated[int, Field(ge=1, le=1_000_000)] = Field(alias="segmentCount")

    @model_validator(mode="after")
    def _validate_identity(self) -> TranscriptReadyEnvelope:
        if self.org_id != self.tenant_id:
            raise ValueError("orgId must equal tenantId for transcript-ready v1")
        if self.generated_at.tzinfo is None:
            raise ValueError("generatedAt must carry a timezone")
        return self


@dataclass(frozen=True)
class ParsedTranscriptReadyEvent:
    event_key: str
    payload_sha256: str
    meeting_id: uuid.UUID
    tenant_id: uuid.UUID
    session_id: uuid.UUID
    finalization_version: int
    segment_count: int
    generated_at: datetime
    analysis_run_id: uuid.UUID
    canonical_read_grant: SecretStr = field(repr=False)

    @property
    def lookup_key(self) -> str:
        return f"{self.tenant_id}|{self.event_key}"


def analysis_run_id_for(
    *,
    tenant_id: uuid.UUID,
    meeting_id: uuid.UUID,
    session_id: uuid.UUID,
    finalization_version: int,
    analysis_spec_version: str,
) -> uuid.UUID:
    """Stable UUIDv5 over the canonical analysis identity tuple."""
    if finalization_version < 1:
        raise ValueError("finalization_version must be positive")
    spec = analysis_spec_version.strip()
    if not spec or len(spec) > 64:
        raise ValueError("analysis_spec_version must contain 1..64 characters")
    name = f"{tenant_id}/{meeting_id}/{session_id}/{finalization_version}/{spec}"
    return uuid.uuid5(_ANALYSIS_RUN_NAMESPACE, name)


def parse_transcript_ready_event(
    fields: dict[object, object],
    *,
    analysis_spec_version: str,
) -> ParsedTranscriptReadyEvent:
    decoded = {_decode(key): value for key, value in fields.items()}
    required = {
        "eventKey",
        "eventType",
        "aggregateId",
        "meetingId",
        "tenantId",
        "orgId",
        "payload",
        "canonicalReadGrant",
    }
    missing = required - decoded.keys()
    if missing:
        raise ReadyEventContractError("ready event is missing required transport fields")

    raw_payload = _bytes(decoded["payload"])
    try:
        envelope = TranscriptReadyEnvelope.model_validate_json(raw_payload)
    except Exception as exc:
        raise ReadyEventContractError("ready event payload is not valid meeting.event.v1") from exc

    event_key = _decode(decoded["eventKey"])
    event_type = _decode(decoded["eventType"])
    aggregate_id = _uuid(decoded["aggregateId"], "aggregateId")
    meeting_id = _uuid(decoded["meetingId"], "meetingId")
    tenant_id = _uuid(decoded["tenantId"], "tenantId")
    org_id = _uuid(decoded["orgId"], "orgId")
    canonical_read_grant = _read_grant(decoded["canonicalReadGrant"])
    expected_key = (
        f"meeting.transcript|{envelope.transcript_session_id}"
        f"|meeting.transcript.ready|{envelope.finalization_version}"
    )
    if not event_key or len(event_key) > 512 or event_key != expected_key:
        raise ReadyEventContractError("ready event has a non-canonical eventKey")
    if event_type != envelope.event_type:
        raise ReadyEventContractError("ready eventType does not match its payload")
    if aggregate_id != envelope.transcript_session_id:
        raise ReadyEventContractError("ready aggregateId does not match its payload")
    if meeting_id != envelope.meeting_id:
        raise ReadyEventContractError("ready meetingId does not match its payload")
    if tenant_id != envelope.tenant_id or org_id != envelope.org_id:
        raise ReadyEventContractError("ready tenant/org identity does not match its payload")

    run_id = analysis_run_id_for(
        tenant_id=envelope.tenant_id,
        meeting_id=envelope.meeting_id,
        session_id=envelope.transcript_session_id,
        finalization_version=envelope.finalization_version,
        analysis_spec_version=analysis_spec_version,
    )
    return ParsedTranscriptReadyEvent(
        event_key=event_key,
        payload_sha256=hashlib.sha256(raw_payload).hexdigest(),
        meeting_id=envelope.meeting_id,
        tenant_id=envelope.tenant_id,
        session_id=envelope.transcript_session_id,
        finalization_version=envelope.finalization_version,
        segment_count=envelope.segment_count,
        generated_at=envelope.generated_at,
        analysis_run_id=run_id,
        canonical_read_grant=canonical_read_grant,
    )


def payload_sha256(fields: dict[object, object]) -> str:
    """Hash the raw payload bytes even when the event is malformed."""
    for key, value in fields.items():
        try:
            if _decode(key) == "payload":
                return hashlib.sha256(_bytes(value)).hexdigest()
        except (UnicodeDecodeError, ReadyEventContractError):
            continue
    return hashlib.sha256(b"").hexdigest()


def stream_event_type(fields: dict[object, object]) -> str | None:
    """Read only the outer routing field; malformed/missing values remain poison."""
    for key, value in fields.items():
        try:
            if _decode(key) == "eventType":
                return _decode(value)
        except (UnicodeDecodeError, ReadyEventContractError):
            continue
    return None


def _uuid(value: object, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(_decode(value))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ReadyEventContractError(f"ready {field} must be a UUID") from exc


def _decode(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="strict")
    if isinstance(value, str):
        return value
    raise ReadyEventContractError("ready event fields must be UTF-8 strings")


def _bytes(value: object) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    raise ReadyEventContractError("ready event payload must be UTF-8 JSON")


def _read_grant(value: object) -> SecretStr:
    try:
        grant = _decode(value)
    except UnicodeDecodeError as exc:
        raise ReadyEventContractError("ready canonical read grant is invalid") from exc
    if (
        len(grant) != 46
        or not grant.startswith("v1.")
        or any(
            character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
            for character in grant[3:]
        )
    ):
        raise ReadyEventContractError("ready canonical read grant is invalid")
    return SecretStr(grant)
