"""Pinned meeting.transcript.ready wire and stable analysis identity tests."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

import pytest

from app.models.ready_event import (
    ReadyEventContractError,
    parse_transcript_ready_event,
    payload_sha256,
)

MEETING = "11111111-1111-4111-8111-111111111111"
SESSION = "22222222-2222-4222-8222-222222222222"
TENANT = "33333333-3333-4333-8333-333333333333"
RUN_ID = "44444444-4444-4444-8444-444444444444"
EVENT_KEY = f"meeting.transcript|{SESSION}|meeting.transcript.ready|1"


def _payload(**overrides: object) -> bytes:
    body: dict[str, object] = {
        "schema": "meeting.event.v1",
        "eventType": "meeting.transcript.ready",
        "analysisRunId": RUN_ID,
        "meetingId": MEETING,
        "tenantId": TENANT,
        "orgId": TENANT,
        "generatedAt": "2026-07-18T01:02:03Z",
        "transcriptSessionId": SESSION,
        "finalizationVersion": 1,
        "segmentCount": 2,
    }
    body.update(overrides)
    return json.dumps(body, separators=(",", ":")).encode()


def _fields(payload: bytes | None = None, **overrides: object) -> dict[object, object]:
    fields: dict[object, object] = {
        b"eventKey": EVENT_KEY.encode(),
        b"eventType": b"meeting.transcript.ready",
        b"aggregateId": SESSION.encode(),
        b"meetingId": MEETING.encode(),
        b"tenantId": TENANT.encode(),
        b"orgId": TENANT.encode(),
        b"payload": payload or _payload(),
    }
    fields.update(overrides)
    return fields


def test_parser_pins_v1_cross_field_identity_and_payload_bytes() -> None:
    raw = _payload()
    event = parse_transcript_ready_event(
        _fields(raw),
        analysis_spec_version="meeting-intelligence-v1",
    )

    assert event.event_key == EVENT_KEY
    assert event.payload_sha256 == hashlib.sha256(raw).hexdigest()
    assert str(event.analysis_run_id) == RUN_ID
    assert event.finalization_version == 1


def test_parser_accepts_vendored_backend_v1_golden_contract() -> None:
    raw = (
        Path(__file__).parents[1] / "contracts" / "backend-transcript-ready-v1.json"
    ).read_bytes()
    payload = json.loads(raw)
    session_id = payload["transcriptSessionId"]
    event = parse_transcript_ready_event(
        {
            "eventKey": (
                f"meeting.transcript|{session_id}|meeting.transcript.ready|"
                f"{payload['finalizationVersion']}"
            ),
            "eventType": payload["eventType"],
            "aggregateId": session_id,
            "meetingId": payload["meetingId"],
            "tenantId": payload["tenantId"],
            "orgId": payload["orgId"],
            "payload": raw,
        },
        analysis_spec_version="meeting-intelligence-v1",
    )

    assert str(event.analysis_run_id) == payload["analysisRunId"]
    assert str(event.tenant_id) == payload["tenantId"]


def test_parser_rejects_transport_read_grant() -> None:
    fields = _fields()
    fields[b"canonicalReadGrant"] = b"must-not-enter-the-stream"
    with pytest.raises(ReadyEventContractError) as captured:
        parse_transcript_ready_event(
            fields,
            analysis_spec_version="meeting-intelligence-v1",
        )
    assert "must-not-enter-the-stream" not in repr(captured.value)


def test_parser_prefers_producer_minted_analysis_job_identity() -> None:
    producer_run_id = uuid.uuid4()
    event = parse_transcript_ready_event(
        _fields(_payload(analysisRunId=str(producer_run_id))),
        analysis_spec_version="meeting-intelligence-v1",
    )

    assert event.analysis_run_id == producer_run_id


@pytest.mark.parametrize(
    ("fields", "payload"),
    [
        ({b"tenantId": uuid.uuid4().bytes}, None),
        ({b"eventKey": b"wrong"}, None),
        ({}, _payload(finalizationVersion=0)),
        ({}, _payload(analysisRunId=None)),
        ({}, _payload(analysisRunId="not-a-uuid")),
        ({}, _payload(orgId=str(uuid.uuid4()))),
    ],
)
def test_parser_rejects_cross_tenant_noncanonical_or_future_contract(
    fields: dict[object, object], payload: bytes | None
) -> None:
    event_fields = _fields(payload)
    event_fields.update(fields)
    with pytest.raises(ReadyEventContractError):
        parse_transcript_ready_event(
            event_fields,
            analysis_spec_version="meeting-intelligence-v1",
        )


def test_parser_accepts_later_finalization_cycle() -> None:
    later_run_id = uuid.uuid4()
    raw = _payload(finalizationVersion=2, analysisRunId=str(later_run_id))
    event = parse_transcript_ready_event(
        _fields(
            raw,
            eventKey=f"meeting.transcript|{SESSION}|meeting.transcript.ready|2",
        ),
        analysis_spec_version="meeting-intelligence-v1",
    )

    assert event.finalization_version == 2
    assert event.event_key.endswith("|2")
    assert event.analysis_run_id == later_run_id


def test_same_semantic_json_with_different_bytes_gets_a_different_hash() -> None:
    compact = _payload()
    spaced = json.dumps(json.loads(compact), indent=2).encode()
    first = parse_transcript_ready_event(
        _fields(compact), analysis_spec_version="meeting-intelligence-v1"
    )
    second = parse_transcript_ready_event(
        _fields(spaced), analysis_spec_version="meeting-intelligence-v1"
    )
    assert first.event_key == second.event_key
    assert first.payload_sha256 != second.payload_sha256


def test_poison_hash_finds_exact_payload_after_malformed_field_key() -> None:
    raw = b"not-json-but-still-exact-wire-bytes"
    fields: dict[object, object] = {
        b"\xff": b"malformed-key",
        b"payload": raw,
    }
    assert payload_sha256(fields) == hashlib.sha256(raw).hexdigest()
