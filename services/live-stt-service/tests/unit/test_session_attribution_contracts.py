"""#3746 AI-D3 — session attribution contract schema invariants.

Machine-checks the two new contract documents so a schema regression fails
the build instead of relying on prose: both schemas parse, are strict
(additionalProperties: false at every object level), pin their const
discriminators, and accept/reject the canonical example payloads. The event
deliberately carries NO text and NO UTF-16 offsets: the producer only knows
window time ranges; the consumer derives the SpeakerAttribution v2 turn from
the stored window text it owns (design D5).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

jsonschema = pytest.importorskip("jsonschema")

CONTRACTS = Path(__file__).resolve().parents[4] / "docs" / "contracts"
ATTRIBUTION = CONTRACTS / "direct-stt-session-attribution.v1.schema.json"
FINISH = CONTRACTS / "live-stt-session-finish.v1.schema.json"

MEETING = "9b2c5a89-f39a-47da-a33d-a2859b468e8b"


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def valid_attribution() -> dict[str, Any]:
    return {
        "schema": "directSttSessionAttribution.v1",
        "tenantId": "42",
        "meetingId": MEETING,
        "sourceSessionId": "SES-1",
        "transportEpoch": 0,
        "model": "pyannote/speaker-diarization-3.1",
        "modelRevision": "84fd25912480287da0247647c3d2b4853cb3ee5d",
        "audioSampleCount": 16000,
        "generatedAt": "2026-09-29T12:00:00Z",
        "windows": [
            {
                "windowSeq": 0,
                "speaker": "SPEAKER_00",
                "startMs": 0,
                "endMs": 4000,
                "dominanceRatio": 0.92,
            },
            {
                "windowSeq": 1,
                "speaker": "UU",
                "startMs": 4000,
                "endMs": 9000,
                "dominanceRatio": 0.51,
            },
        ],
    }


def valid_finish() -> dict[str, Any]:
    return {
        "schema": "liveSttSessionFinish.v1",
        "tenantId": "42",
        "meetingId": MEETING,
        "sourceSessionId": "SES-1",
        "transportEpoch": 0,
        "expectedSampleCount": 16000,
    }


def assert_strict_objects(node: Any, path: str = "$") -> None:
    """Every object schema must close itself (additionalProperties: false)."""
    if isinstance(node, dict):
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False, f"{path} is not strict"
        for key, value in node.items():
            assert_strict_objects(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            assert_strict_objects(value, f"{path}[{i}]")


@pytest.mark.parametrize("path", [ATTRIBUTION, FINISH])
def test_schema_parses_and_is_strict(path: Path) -> None:
    schema = load(path)
    jsonschema.Draft202012Validator.check_schema(schema)
    assert_strict_objects(schema)


def test_attribution_example_validates() -> None:
    jsonschema.validate(valid_attribution(), load(ATTRIBUTION))


def test_finish_example_validates() -> None:
    jsonschema.validate(valid_finish(), load(FINISH))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.pop("windows"),
        lambda p: p.update(schema="directSttSessionAttribution.v2"),
        lambda p: p.update(model="pyannote/speaker-diarization-3.0"),
        lambda p: p.update(extra="x"),
        lambda p: p["windows"][0].update(speaker="ALICE"),
        lambda p: p["windows"][0].update(voiceprint="x"),
        lambda p: p["windows"][0].update(text="never carry transcript text"),
        lambda p: p["windows"][0].update(textStart=0),
        lambda p: p["windows"][0].pop("dominanceRatio"),
        lambda p: p.update(windows=[]),
        lambda p: p.update(audioSampleCount=0),
        lambda p: p.update(meetingId="not-a-uuid"),
    ],
)
def test_attribution_rejects_contract_violations(mutate) -> None:
    payload = valid_attribution()
    mutate(payload)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, load(ATTRIBUTION))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.pop("expectedSampleCount"),
        lambda p: p.update(schema="liveSttSessionFinish.v2"),
        lambda p: p.update(audio="raw-bytes-do-not-belong-here"),
        lambda p: p.update(expectedSampleCount=0),
    ],
)
def test_finish_rejects_contract_violations(mutate) -> None:
    payload = valid_finish()
    mutate(payload)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, load(FINISH))
